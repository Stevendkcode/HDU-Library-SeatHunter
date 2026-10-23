def pwinput(prompt="Password: ", mask="*"):
    import sys
    print(prompt, end="", flush=True)
    return sys.stdin.readline().strip()
