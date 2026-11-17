# Test and lint team-build, and render the site's social preview.
#
#   make test   run the board self-test in a temp repo
#   make lint   ruff on board.py, shellcheck on the self-test, JSON manifests
#   make og     render site/og.png from design/og.html with headless Chrome
#   make serve  serve site/ on http://localhost:8000

SKILL := plugin/skills/team-build
RUFF ?= uvx --system-certs ruff
CHROME ?= /Applications/Google Chrome.app/Contents/MacOS/Google Chrome

.PHONY: test lint og serve

test:
	bash $(SKILL)/scripts/selftest.sh

lint:
	$(RUFF) check --select E,F,W,B --line-length 140 --target-version py39 $(SKILL)/scripts/board.py
	shellcheck $(SKILL)/scripts/selftest.sh
	@for f in .claude-plugin/marketplace.json plugin/plugin.json; do \
		python3 -m json.tool $$f >/dev/null || exit 1; echo "ok $$f"; done

og:
	python3 design/og.py
	"$(CHROME)" --headless=new --hide-scrollbars --force-device-scale-factor=1 \
		--window-size=1200,630 --virtual-time-budget=6000 \
		--screenshot=$(CURDIR)/site/og.png file://$(CURDIR)/design/og.build.html
	rm -f design/og.build.html

serve:
	python3 -m http.server --directory site 8000
