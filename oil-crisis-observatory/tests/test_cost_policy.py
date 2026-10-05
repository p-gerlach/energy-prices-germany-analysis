"""Acceptance: zero-charge policy enforcement in the guarded HTTP client."""
import re
from pathlib import Path

import httpx
import pytest

from oco.http import (AuthRejected, ConnectorStopped, ContentValidationError, GuardedClient, NetworkUnavailable,
                      RateLimited, redact)
from oco.policy import PolicyDenied, PolicyError, load_policy, policy_from_dict

SRC = Path(__file__).resolve().parents[1] / "src"
PORTWATCH = "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/Daily_Chokepoints_Data/FeatureServer/0"


def client(state, connector, handler, **kw):
    sent = []

    def h(req):
        sent.append(req)
        return handler(req)
    c = GuardedClient(connector, state, transport=httpx.MockTransport(h), sleep=lambda s: None, **kw)
    return c, sent


def ok_json(req):
    return httpx.Response(200, json={"features": []})


@pytest.mark.parametrize("connector,method,url", [
    ("portwatch", "GET", "https://services9.arcgis.com/OTHERORG/arcgis/rest/services/X/FeatureServer/0/query"),
    ("portwatch", "GET", "https://www.arcgis.com/sharing/rest/generateToken"),
    ("portwatch", "POST", PORTWATCH + "/query"),                       # only GET is approved
    ("portwatch", "GET", PORTWATCH + "/applyEdits"),                   # write op
    ("portwatch", "GET", PORTWATCH + "/query?where=1%3D1&token=abc"),  # ArcGIS token forbidden
    ("cdse_catalogue", "GET", "https://sh.dataspace.copernicus.eu/api/v1/process"),
    ("cdse_catalogue", "POST", "https://openeo.dataspace.copernicus.eu/openeo/1.2/jobs"),
    ("gdelt", "GET", "https://bigquery.googleapis.com/bigquery/v2/projects/gdelt-bq/queries"),
    ("ecb", "GET", "http://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"),  # plain http
    ("ecb", "GET", "https://data-api.ecb.europa.eu/service/data/ICP/M.U2.N.000000.4.ANR"),  # other dataset
    ("eia", "GET", "https://api.eia.gov/v2/electricity/retail-sales/data"),
    ("rss", "GET", "https://www.spiegel.de/plus/index.rss"),
    ("local_llm", "POST", "https://api.openai.com/v1/chat/completions"),
    ("unknown_connector", "GET", "https://example.org/"),
])
def test_unapproved_routes_blocked_before_request(state, connector, method, url):
    sent = []
    if connector == "unknown_connector":
        with pytest.raises(PolicyDenied):
            GuardedClient(connector, state, transport=httpx.MockTransport(lambda r: sent.append(r)))
        return
    c, sent = client(state, connector, ok_json)
    with pytest.raises(PolicyDenied):
        c.request(method, url)
    assert sent == [], "request must be blocked BEFORE any bytes are sent"


def test_approved_portwatch_query_is_anonymous(state):
    c, sent = client(state, "portwatch", ok_json)
    r = c.get(PORTWATCH + "/query", params={"where": "1=1", "f": "json"})
    assert r.status == 200
    assert "authorization" not in {k.lower() for k in sent[0].headers}
    assert "token" not in str(sent[0].url)


def test_redirect_to_unapproved_host_blocked(state):
    calls = []

    def h(req):
        calls.append(str(req.url))
        return httpx.Response(302, headers={"location": "https://evil.example.com/upgrade"})
    c, _ = client(state, "ecb", h)
    with pytest.raises(PolicyDenied):
        c.get("https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A")
    assert len(calls) == 1  # redirect target never contacted


def test_redirect_within_approved_route_strips_credentials(state):
    seen = []

    def h(req):
        seen.append((req.url.host, req.headers.get("authorization")))
        if req.url.host == "catalogue.dataspace.copernicus.eu":
            return httpx.Response(302, headers={"location": "https://zipper.dataspace.copernicus.eu/odata/v1/Products(11111111-2222-3333-4444-555555555555)/$value"})
        return httpx.Response(200, content=b"PK\x03\x04", headers={"content-type": "application/zip"})
    c, _ = client(state, "cdse_download", h)
    r = c.get("https://catalogue.dataspace.copernicus.eu/odata/v1/Products(11111111-2222-3333-4444-555555555555)/$value",
              auth_header="Bearer TESTTOKEN")
    assert r.status == 200
    assert seen[0][1] == "Bearer TESTTOKEN" and seen[1][0] == "zipper.dataspace.copernicus.eu"


def test_credentials_never_sent_on_non_credential_route(state):
    c, sent = client(state, "ecb", ok_json)
    with pytest.raises(PolicyDenied):
        c.get("https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A", auth_header="Bearer x")
    assert sent == []


@pytest.mark.parametrize("resp", [
    httpx.Response(402, text="Payment Required"),
    httpx.Response(200, text="<html>Your trial has expired. Upgrade your plan.</html>", headers={"content-type": "text/html"}),
    httpx.Response(403, text="Insufficient processing units", headers={"content-type": "text/plain"}),
    httpx.Response(401, text="please log in"),   # anonymous route suddenly demands auth
])
def test_payment_or_entitlement_prompts_stop_connector(state, resp):
    c, sent = client(state, "portwatch", lambda r: resp)
    with pytest.raises(ConnectorStopped):
        c.get(PORTWATCH + "/query", params={"where": "1=1", "f": "json"})
    assert state.connector_status("portwatch")["status"] == "stopped"
    n = len(sent)
    with pytest.raises(ConnectorStopped):  # stays stopped; no further requests
        c.get(PORTWATCH + "/query", params={"where": "1=1", "f": "json"})
    assert len(sent) == n


def test_429_backs_off_then_pauses_never_stops(state):
    waits = []
    c = GuardedClient("ecb", state, transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"retry-after": "7"})),
                      sleep=waits.append, max_retries=2)
    with pytest.raises(RateLimited):
        c.get("https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A")
    assert waits == [7.0, 7.0]
    st = state.connector_status("ecb")
    assert st["status"] == "paused" and st["status"] != "stopped"


def test_429_then_success(state):
    seq = iter([httpx.Response(429), httpx.Response(200, text="KEY,TIME_PERIOD,OBS_VALUE\n", headers={"content-type": "text/csv"})])
    c = GuardedClient("ecb", state, transport=httpx.MockTransport(lambda r: next(seq)), sleep=lambda s: None)
    assert c.get("https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A").status == 200


def test_html_error_page_with_200_rejected(state):
    c, _ = client(state, "ecb", lambda r: httpx.Response(200, text="<html>Service down</html>", headers={"content-type": "text/html"}))
    with pytest.raises(ContentValidationError):
        c.get("https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A")
    assert state.connector_status("ecb")["status"] == "active"


def test_proxy_denial_is_network_unavailable_not_provider_stop(state):
    def h(req):
        raise httpx.ProxyError("403 Forbidden")
    c, _ = client(state, "ecb", h)
    with pytest.raises(NetworkUnavailable):
        c.get("https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A")
    assert state.connector_status("ecb")["status"] == "active"


def test_timeouts_retry_and_open_circuit(state):
    def h(req):
        raise httpx.ReadTimeout("slow")
    c = GuardedClient("ecb", state, transport=httpx.MockTransport(h), sleep=lambda s: None, max_retries=1)
    for _ in range(5):
        with pytest.raises(Exception):
            c.get("https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A")
    assert state.connector_status("ecb")["circuit_open_until"] is not None


def test_credential_401_is_auth_rejected_for_credential_route(state):
    c, _ = client(state, "eia", lambda r: httpx.Response(401, text="bad key"), secrets=["SECRETKEY123456"])
    with pytest.raises(AuthRejected):
        c.get("https://api.eia.gov/v2/petroleum/pri/spt/data", params={"api_key": "SECRETKEY123456"})


def test_size_limit(state):
    c, _ = client(state, "ecb", lambda r: httpx.Response(200, content=b"x" * 2000, headers={"content-type": "text/csv"}))
    with pytest.raises(ContentValidationError):
        c.get("https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A", max_bytes=1000)


def test_redaction_of_keys_in_urls():
    u = "https://api.eia.gov/v2/petroleum/pri/spt/data?api_key=ABCDEF123456&x=1"
    assert "ABCDEF123456" not in redact(u)
    f = "https://firms.modaps.eosdis.nasa.gov/api/area/csv/0123456789abcdef0123456789abcdef/VIIRS_SNPP_NRT/1,2,3,4/1"
    assert "0123456789abcdef0123456789abcdef" not in redact(f, ["0123456789abcdef0123456789abcdef"])


def test_policy_file_is_strict():
    p = load_policy()
    assert p.raw["default"] == "deny"
    for c in p.connectors.values():
        assert c.meta["payment_required"] is False
        assert c.meta["processing_credit_dependence"] is False
        assert str(c.meta["trial_expiry"]) == "none"
    # PortWatch: exactly one service path, no wildcard over arcgis.com
    pw = p.connectors["portwatch"].routes
    allowed_layers = {"Daily_Chokepoints_Data", "Daily_Ports_Data"}  # each added after an explicit review
    assert pw and all(r.host == "services9.arcgis.com" and r.methods == ("GET",) for r in pw)
    for r in pw:
        pat = r.path_regex.pattern
        assert pat.startswith("^/weJ1QsnbMYJlCHdG/arcgis/rest/services/") and "FeatureServer/0" in pat
        assert any(f"/services/{layer}/" in pat for layer in allowed_layers), pat
        assert ".*" not in pat and "[^/]+" not in pat, "no wildcard over arcgis services"
    assert p.connectors["portwatch"].credential == "none"


def test_policy_rejects_paid_switch_or_payment_flag():
    raw = load_policy().raw
    import copy
    bad = copy.deepcopy(raw)
    bad["allow_paid"] = True
    with pytest.raises(PolicyError):
        policy_from_dict(bad)
    bad = copy.deepcopy(raw)
    bad["connectors"]["ecb"]["payment_required"] = True
    with pytest.raises(PolicyError):
        policy_from_dict(bad)
    bad = copy.deepcopy(raw)
    bad["connectors"]["ecb"]["routes"].append({"host": "sh.dataspace.copernicus.eu", "methods": ["GET"], "path_regex": ".*"})
    with pytest.raises(PolicyError):
        policy_from_dict(bad)


def test_no_account_creation_payment_or_cloud_code():
    """Static scan: no registration/billing/cloud-provisioning endpoints or SDKs in the source tree."""
    forbidden = [
        r"https?://[^\s'\"]*/register(?!\.php)", r"signup", r"sign_up", r"https?://[^\s'\"]*/billing", r"checkout", r"stripe", r"purchase\(",
        r"https?://[^\s'\"]*(subscribe|upgrade|pricing)",
        r"generateToken", r"arcgis\.gis", r"from arcgis", r"import arcgis", r"sentinelhub", r"import openeo",
        r"bigquery", r"boto3", r"google\.cloud", r"azure", r"openai", r"anthropic",
    ]
    allowed_files = {"policy.py"}  # policy names excluded hosts/env prefixes to deny them
    hits = []
    for f in SRC.rglob("*.py"):
        if f.name in allowed_files:
            continue
        text = f.read_text()
        for pat in forbidden:
            for m in re.finditer(pat, text, re.I):
                line = text[: m.start()].count("\n") + 1
                ctx = text.splitlines()[line - 1]
                if "never" in ctx.lower() or "no " in ctx.lower() or "not " in ctx.lower() or "excluded" in ctx.lower():
                    continue  # documentation of the prohibition itself
                hits.append(f"{f.name}:{line}: {ctx.strip()[:100]}")
    assert hits == [], "forbidden provisioning/billing code found:\n" + "\n".join(hits)


def test_app_never_reads_cloud_credentials(monkeypatch):
    from oco import settings
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    monkeypatch.setenv("ARCGIS_API_KEY", "x")
    with pytest.raises(KeyError):
        settings.env("AWS_SECRET_ACCESS_KEY")
    with pytest.raises(KeyError):
        settings.env("ARCGIS_API_KEY")


def test_firewall_throttle_pauses_but_login_demand_still_stops(state):
    body = '{"status":"error","data":{"message":"This request was rejected due to a violation. Please consult with your administrator"}}'
    c, _ = client(state, "cdse_catalogue", lambda r: httpx.Response(403, text=body, headers={"content-type": "application/json"}))
    with pytest.raises(RateLimited):
        c.get("https://catalogue.dataspace.copernicus.eu/odata/v1/Products")
    assert state.connector_status("cdse_catalogue")["status"] == "paused"
    c2, _ = client(state, "portwatch", lambda r: httpx.Response(403, text="Please sign in", headers={"content-type": "text/plain"}))
    with pytest.raises(ConnectorStopped):
        c2.get(PORTWATCH + "/query", params={"where": "1=1", "f": "json"})
