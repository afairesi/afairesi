import os
import sys


def main():
    print(os.environ.get("AFAIRESI_CONSUMER_HOOK", "unloaded"))
    print(repr(sys.argv[1:]))
