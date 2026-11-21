"""Put the repo root on sys.path (the existing tests do the same) so `enterprise` and `harness` import."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
