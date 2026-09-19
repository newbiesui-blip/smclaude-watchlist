from coinglass_client import CoinGlassClient, _base_symbol, _pair_symbol


def test_symbol_normalization():
    assert _base_symbol("ZEC-USDT") == "ZEC"
    assert _base_symbol("ZECUSDT") == "ZEC"
    assert _pair_symbol("ZEC-USDT") == "ZECUSDT"


def test_missing_api_key_is_safe(monkeypatch):
    monkeypatch.delenv("COINGLASS_API_KEY", raising=False)
    client = CoinGlassClient(api_key=None)
    result = client.snapshot("ZEC-USDT")
    assert result["enabled"] is False
    assert result["availability"] == "NOT_CONFIGURED"


def test_snapshot_parses_read_only_sources():
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"code": "0", "data": [{"close": "100"}]}

    class FakeSession:
        def get(self, *args, **kwargs):
            return FakeResponse()

    client = CoinGlassClient(api_key="test", session=FakeSession())
    result = client.snapshot("ZEC-USDT")
    assert result["enabled"] is True
    assert result["availability"] == "OK"
    assert result["sources"]["oi"] == "OK"
