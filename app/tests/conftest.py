import os

# During test runs, target example configs instead of personal gitignored configs
os.environ.setdefault("FILTERS_CONFIG_PATH", "filters.example.yaml")
