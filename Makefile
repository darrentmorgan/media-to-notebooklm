.PHONY: venv install update-ytdlp check

venv:
	python3 -m venv .venv
	./.venv/bin/pip install --upgrade pip
	PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 ./.venv/bin/pip install -e .

install:
	PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 ./.venv/bin/pip install -e .

update-ytdlp:
	./.venv/bin/pip install --upgrade yt-dlp

check:
	git diff --check
	python3 -m compileall -q src
	bin/yt-to-nblm --help > /dev/null
