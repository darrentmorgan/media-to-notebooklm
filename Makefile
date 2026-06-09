REPO := $(shell pwd)
PLIST_LABEL := com.user.mediatonblm
PLIST_SRC   := telegram-bridge/launchd/$(PLIST_LABEL).plist.template
PLIST_OUT   := telegram-bridge/launchd/$(PLIST_LABEL).plist
PLIST_DEST  := $(HOME)/Library/LaunchAgents/$(PLIST_LABEL).plist

.PHONY: venv install-deps bot run-bot install-launchd uninstall-launchd reload-launchd bot-status update-ytdlp

venv:
	python3 -m venv .venv
	./.venv/bin/pip install --upgrade pip
	./.venv/bin/pip install click python-dotenv yt-dlp 'python-telegram-bot>=21'

install-deps:
	./.venv/bin/pip install click python-dotenv yt-dlp 'python-telegram-bot>=21'

run-bot:
	./.venv/bin/python telegram-bridge/bot.py

$(PLIST_OUT): $(PLIST_SRC)
	sed 's|__REPO__|$(REPO)|g' $< > $@

install-launchd: $(PLIST_OUT)
	mkdir -p $(HOME)/Library/LaunchAgents out
	cp $(PLIST_OUT) $(PLIST_DEST)
	launchctl unload $(PLIST_DEST) 2>/dev/null || true
	launchctl load $(PLIST_DEST)
	@echo "installed → $(PLIST_DEST)"
	@launchctl list | grep $(PLIST_LABEL) || echo "not listed yet — check $(REPO)/out/bot.stderr.log"

reload-launchd:
	launchctl unload $(PLIST_DEST) 2>/dev/null || true
	launchctl load $(PLIST_DEST)

uninstall-launchd:
	launchctl unload $(PLIST_DEST) 2>/dev/null || true
	rm -f $(PLIST_DEST)
	@echo "removed $(PLIST_DEST)"

bot-status:
	@launchctl list | grep $(PLIST_LABEL) || echo "not loaded"
	@echo "---- stderr tail ----"
	@tail -n 20 out/bot.stderr.log 2>/dev/null || echo "(no log yet)"

update-ytdlp:
	./.venv/bin/pip install --upgrade yt-dlp
