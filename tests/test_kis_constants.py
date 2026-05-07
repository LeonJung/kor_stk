from ks_ws.kis.constants import OAUTH_TOKEN_PATH, REST_BASE_URL, rest_url


def test_mock_and_live_urls_differ():
    assert REST_BASE_URL["mock"] != REST_BASE_URL["live"]
    assert REST_BASE_URL["mock"].startswith("https://")
    assert REST_BASE_URL["live"].startswith("https://")


def test_rest_url_concatenates():
    url = rest_url("mock", OAUTH_TOKEN_PATH)
    assert url == REST_BASE_URL["mock"] + OAUTH_TOKEN_PATH
