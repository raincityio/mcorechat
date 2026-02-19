from html.parser import HTMLParser


class HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.result: list[str] = []

    def handle_data(self, data: str):
        self.result.append(data)

    def get_data(self):
        return "".join(self.result)


def strip_html(html: str) -> str:
    s = HTMLStripper()
    s.feed(html)
    return s.get_data()
