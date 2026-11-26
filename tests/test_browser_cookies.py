from adofai_publisher.browser import is_context_closed, tuf_cookies


class FakeContext:
    def __init__(self, cookies_by_url, pages=None):
        self.cookies_by_url = cookies_by_url
        self.pages = pages if pages is not None else [object()]

    def cookies(self, url):
        return self.cookies_by_url.get(url, [])


def test_tuf_cookies_include_api_subdomain_session():
    context = FakeContext(
        {
            "https://tuforums.com/": [
                {"domain": ".tuforums.com", "name": "tuf.clientPrefs", "value": "1"},
            ],
            "https://api.tuforums.com/": [
                {"domain": "api.tuforums.com", "name": "accessToken", "value": "token"},
                {"domain": "api.tuforums.com", "name": "csrfToken", "value": "csrf"},
            ],
        }
    )

    cookies = tuf_cookies(context)

    assert cookies["accessToken"] == "token"
    assert cookies["csrfToken"] == "csrf"
    assert cookies["tuf.clientPrefs"] == "1"


def test_context_closed_when_no_pages():
    assert is_context_closed(FakeContext({}, pages=[])) is True
    assert is_context_closed(FakeContext({}, pages=[object()])) is False


def test_context_closed_when_pages_raises():
    class BrokenContext:
        @property
        def pages(self):
            raise RuntimeError("Target closed")

    assert is_context_closed(BrokenContext()) is True
