"""Read plain text from the locally expanded SW HelpViewer archive."""
from html.parser import HTMLParser
import sys
import zipfile


class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden -= 1

    def handle_data(self, text):
        if not self.hidden and text.strip():
            self.parts.append(text.strip())


with zipfile.ZipFile(".cache/sw-api/apihelpviewer.cab") as archive:
    for pattern in sys.argv[1:]:
        for name in archive.namelist():
            if pattern.casefold() in name.casefold():
                parser = Text()
                parser.feed(archive.read(name).decode("utf-8-sig", errors="replace"))
                print(name, "\n", "\n".join(parser.parts))
