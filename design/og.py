"""Write design/og.build.html: design/og.html with the track diagram from site/index.html."""
import pathlib
import re

here = pathlib.Path(__file__).parent
page = (here.parent / "site" / "index.html").read_text()
yard = re.search(r'<svg id="yard".*?</svg>', page, re.S).group(0)
out = (here / "og.html").read_text().replace("<!-- YARD -->", yard)
(here / "og.build.html").write_text(out)
