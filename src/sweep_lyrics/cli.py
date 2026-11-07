import runpy


def main():
    runpy.run_module("sweep_lyrics.app", run_name="__main__", alter_sys=True)
