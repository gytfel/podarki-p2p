from giftbot.diag import cloudflare_verdict, shape

BLOCKED = """<!DOCTYPE html> <!--[if lt IE 7]> <html class="no-js ie6 oldie" lang="en-US"> <![endif]-->
<title>Attention Required! | Cloudflare</title>
<h1 data-translate="block_headline">Sorry, you have been blocked</h1>
<span class="cf-footer-item">Cloudflare Ray ID: <strong class="font-semibold">8a1b2c3d4e5f6a7b</strong></span>"""

COUNTRY = """<title>Access denied | gifts3.tonnel.network used Cloudflare to restrict access</title>
<h1><span data-translate="error">Error</span><span class="cf-error-code">1009</span></h1>
<h2 class="cf-subheadline">The owner of this website (gifts3.tonnel.network) has banned the country or region
your IP address is in (RU) from accessing this website.</h2>"""


def test_cloudflare_block_page_is_recognized():
    verdict = cloudflare_verdict(403, BLOCKED, {})
    assert verdict.startswith("Cloudflare: доступ заблокирован") and "8a1b2c3d4e5f6a7b" in verdict


def test_country_ban_names_the_country():
    verdict = cloudflare_verdict(403, COUNTRY, {})
    assert "1009" in verdict and "страна" in verdict and "(RU)" in verdict


def test_js_challenge_and_plain_errors():
    assert "JS-challenge" in cloudflare_verdict(403, "<title>Just a moment...</title>", {})
    assert cloudflare_verdict(403, '{"message": "bad auth"}', {}).startswith("не Cloudflare")
    assert cloudflare_verdict(200, "{}", {}) == "ok"


def test_shape_shows_keys_and_samples_without_dumping_everything():
    data = {"floor_prices": {"toybear": {"models": {f"M{i}": str(i) for i in range(12)}}}}
    text = shape(data)
    assert "'floor_prices'" in text and "'toybear'" in text and "… ещё" not in text.split("'models'")[0]
    assert shape([]) == "[]" and shape([{"a": 1}]).startswith("[1 шт.")
    assert shape("x" * 100).endswith("…")


def test_plain_text_cloudflare_answer_is_recognized():
    assert cloudflare_verdict(403, "error code: 1020", {}).startswith("Cloudflare")
