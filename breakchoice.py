import enum
import inspect
from itertools import dropwhile
import json
import logging
import os
from os.path import expanduser, exists
import pdb
import subprocess


class AutoStrEnum(str, enum.Enum):
    def _generate_next_value_(name, start, count, last_values):
        return name

class Mode(AutoStrEnum):
    NEVER = enum.auto() # 'NEVER' # Never breakpoint
    FIRST = enum.auto() # 'FIRST' # Trace on first breakpoint
    ALWAYS = enum.auto() # 'ALWAYS' # Always breakpoint via pdb.set_trace()
    AFTER_N = enum.auto() # 'AFTER_N' # After n hits, trace.
    EVERY_N = enum.auto() # 'EVERY_N' # Trace on every n hits

# FIXME: if prompt/xprompt used with other Modes, what about if prompt says no in grouped_counts data?
class Action(AutoStrEnum):
    TRACE = enum.auto() # 'TRACE' - use pdb.set_trace
    XPROMPT = enum.auto() # 'XPROMPT' # window prompt to continue via Zenity
    PROMPT = enum.auto() # 'PROMPT' # terminal prompt to continue (empty line+enter or something else)

def config_loglevel(value: str, default: int):
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return logging.getLevelName(value.upper()) if value else default

def str_to_enums(rules: dict) -> dict:
    """
    Convert json-loaded config rules dict of literals into Mode/Action enums & fix default null/none path.
    
    Converts in place, also lists->tuples, and returns converted dict.
    """
    def convert_value(value):
        if isinstance(value, str):
            try:
                return Mode(value)
            except ValueError:
                return Action(value)
    default_path = []
    for path,behaviours in rules.items():
        if path.lower() in ("null", "none"): # "" not a valid json object key
            default_path.append(path)
        if isinstance(behaviours, str):
            rules[path] = convert_value(behaviours)
        elif isinstance(behaviours, (tuple,list)):
            rules[path] = tuple(convert_value(value) for value in behaviours)
    if default_path:
        if len(default_path) > 1 or (default_path and None in rules):
            raise ValueError("duplicate behaviour set for default path in .cf rules file (null/none keys)")
        default_path = default_path[0]
        behaviour = rules[default_path]
        del rules[default_path]
        rules[None] = behaviour
    return rules


CALL_COUNT = int(os.environ['BC_CALL_COUNT']) if os.environ.get('BC_CALL_COUNT', False) else None
try:
    RULES_FILE = next(dropwhile(lambda p: not exists(expanduser(p)), ("~/.config/breakchoice.cf", "~/.breakchoice.cf", "/etc/breakchoice.cf")))
except StopIteration:
    RULES_FILE = None
STATE_FILE = os.environ.get('BC_STATEFILE', expanduser("~/.local/var/breakchoice.state"))
RECORD_CALLS_FILE = os.environ.get('BC_LOG_CALLS', expanduser('~/.local/var/breakchoice.log')) # may need to rotate filename after lots of usage?
logging.basicConfig(level=config_loglevel(os.environ.get('BC_LOGLEVEL'), logging.INFO))
log = logging.getLogger(__name__)

break_at = str_to_enums(json.load(open(expanduser(RULES_FILE), "r"))) if RULES_FILE else {None: Mode.NEVER}
# break_at = {
# 'filename:line' : Mode.NEVER | (Mode.<mode>, Action.<action>, [<count : int>]) # if action not specified, uses default pdb trace
# e.g.:
#'Controller.py:495': (Mode.AFTER_N, 3),
#'Controller:add_to_instrument': (Mode.AFTER_N, 3),
# }

def load_grouped_counts():
    global grouped_counts
    try:
        grouped_counts
    except NameError:
        pass
    else:
        # Already loaded.
        return
    try:
        import pdb; pdb.set_trace()
        grouped_counts = dict(s.split("/") for s in open(STATE_FILE).readlines())
    except IOError:
        log.debug("no grouped counts state file %r", STATE_FILE)
        grouped_counts = {}

def save_grouped_counts():
    open(STATE_FILE, "w").writelines("\n".join(f"{m}/{c}" for m,c in grouped_counts.items()))

def break_n():
    global CALL_COUNT, grouped_counts

    if CALL_COUNT:
        CALL_COUNT -= 1
        if CALL_COUNT == 0:
            import pdb
            pdb.set_trace()
    else:
        curr = inspect.currentframe()
        caller = inspect.getouterframes(curr)[1]
        basename = os.path.basename(caller.filename)
        resolved_filename = os.path.realpath(caller.filename)
        modulename = basename.rsplit(".", 1)[0]
        parents = []
        spec = caller.frame.f_globals['__spec__']
        if spec:
            while spec:
                parentpackage = spec.parent
                log.debug("..adding package name to search: %s to base %s",parentpackage.__name__,parents[-1])
                parents.append(f"{parentpackage.__name__}.{parents[-1]}")
                spec = parentpackage
            full_packages = ".".join(parents)
        else:
            parentpackage = os.path.basename(os.path.dirname(caller.filename))
            full_packages = f"{parentpackage}.{modulename}" # likely incomplete
        #import pdb; pdb.set_trace() #############################
        lookup_keys = (
            # In such order that earlier, more-specific pattern will override a less-specific one (later) - checked in sequence
            f"{caller.filename}:{caller.lineno}",
            f"{resolved_filename}:{caller.lineno}", # in case of symlinks
            f"{full_packages}.{caller.function}:{caller.lineno}",
            f"{full_packages}.{caller.function}",
            f"{parentpackage}.{modulename}.{caller.function}:{caller.lineno}",
            f"{parentpackage}.{modulename}:{caller.lineno}",
            f"{parentpackage}.{basename}:{caller.lineno}",
            f"{parentpackage}.{basename}",
            f"{parentpackage}.{modulename}.{caller.function}",
            f"{parentpackage}.{modulename}",
            f"{basename}:{caller.lineno}",
            f"{modulename}.{caller.function}",
            caller.filename,
            basename,
            modulename,
            None
        )
        if RECORD_CALLS_FILE:
            open(RECORD_CALLS_FILE, "a").write(f"{caller.filename!r}:{caller.lineno}/{full_packages}:{caller.function}\n")
        log.debug("Looking up for %s", lookup_keys)
        for code_position in lookup_keys:
            log.debug("checking %s", code_position)
            try:
                behaviour = break_at[code_position]
            except KeyError:
                continue

            log.info("Found behaviour spec for %s, action=%s", code_position, behaviour)
            action = None
            mode = None
            count = None
            if not isinstance(behaviour, (tuple,list)):
                behaviour = [behaviour]
            for act in behaviour:
                match act:
                    case Mode(): mode = act
                    case Action(): action = act
                    case int: count = act
                    #case _: raise TypeError(f"Only Mode, Action, and integer count values allowed for path ({code_position})")
             # Set up defaults
            if mode is None:
                if action is None:
                    if count is None:
                        mode = Mode.NEVER # default if no actions
                    else:
                        mode = Mode.AFTER_N # default if just count set
                else:
                    if count is None:
                        mode = Mode.ALWAYS
                    else:
                        mode = Mode.AFTER_N # action+count set
            if action is None:
                action = Action.TRACE # default

            if mode not in (Mode.NEVER, Mode.ALWAYS):
                log.debug("loading grouped counts from disk")
                load_grouped_counts()

            if mode == Mode.AFTER_N:
                try:
                    grouped_counts[code_position] += 1
                except KeyError:
                    grouped_counts[code_position] = 1
                save_grouped_counts()
                if grouped_counts[code_position] < count:
                    return None
            elif mode == Mode.EVERY_N:
                try:
                    grouped_counts[code_position] -= 1
                except KeyError:
                    grouped_counts[code_position] = count - 1
                save_grouped_counts()
                if grouped_counts[code_position]:
                    return None
            elif mode == Mode.NEVER:
                return None

            elif mode == Mode.FIRST:
                try:
                    # Means done, 0 or none saved means yet to do.
                    assert grouped_counts[code_position] == 1
                except (KeyError, AssertionError):
                    grouped_counts[code_position] = 1
                    save_grouped_counts()
                else:
                    # Done action once before, no more.
                    return None
            elif mode == Mode.ALWAYS:
                pass

            # If still here, must need to perform the trap action (otherwise returns None)
            if action == Action.TRACE:
                import pdb
                pdb.set_trace()
            elif action == Action.XPROMPT:
                load_grouped_counts()
                # Whatever mode says, this action can override whether it prompts next time if it does this time.
                try:
                    old_result = grouped_counts[code_position]
                except KeyError:
                    # initialise state, defaults to prompting
                    old_result = grouped_counts[code_position] = 1
                new_result = subprocess.call(["zenity","--question","--text='Pause next time?'"])
                if new_result != old_result:
                    grouped_counts[code_position] = new_result
                    save_grouped_counts()
            elif action == Action.PROMPT:
                load_grouped_counts()
                # Whatever mode says, this action can override whether it prompts next time if it does this time.
                try:
                    old_result = grouped_counts[code_position]
                except KeyError:
                    # initialise state, defaults to prompting
                    old_result = grouped_counts[code_position] = 1
                new_result = input("Continue? (press Enter, or type anything then Enter to cancel prompting next time)")
                if new_result != old_result:
                    grouped_counts[code_position] = new_result
                    save_grouped_counts()
            else:
                log.error("Unhandled action type")
                return None
        log.debug("No matching code position assigned an action and no default, using hardcoded ignore default")
        return None
