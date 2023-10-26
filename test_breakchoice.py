import os

def test_breakchoice():
    for n in range(3):
        breakpoint()
    print("Passed the loop of breakpoint calls")
    ...
    pass
    breakpoint()

def gui():
    breakpoint()

os.environ['PYTHONBREAKPOINT'] = 'breakchoice.break_n'
os.environ['BC_LOGLEVEL'] = 'DEBUG'
breakpoint()
test_breakchoice()
gui()
