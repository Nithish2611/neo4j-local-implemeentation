"""Entry point when running from a clone:  python main.py --help"""
import sys

from cognitive_graph.cli import main

if __name__ == "__main__":
    sys.exit(main())
