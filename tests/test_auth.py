import pytest

from factotum.auth import SESSION_COOKIE, Authenticator
from factotum.users import UserStore, hash_password, verify_password


@pytest.fixture
def store(tmp_path):
    s = UserStore(tmp_path / "users.json")
    s.create_local("anna", "secret", "Anna", admin=True)
    return s


def _auth(store, providers=("local", "ha_ingress", "api_token")):
    return Authenticator(store, list(providers), "key", api_token="tok123", trusted_proxies=["172.30.32.2"])


def test_password_hash():
    h = hash_password("x")
    assert verify_password("x", h) and not verify_password("y", h)


def test_login_and_session(store):
    auth = _auth(store)
    user = auth.login("anna", "secret")
    assert user and auth.login("anna", "wrong") is None
    cookie = auth.create_session(user)
    result = auth.resolve({}, "10.0.0.5", {SESSION_COOKIE: cookie})
    assert result.user is user and result.provider == "local"
    tampered = cookie[:-4] + "AAAA"
    assert auth.resolve({}, "10.0.0.5", {SESSION_COOKIE: tampered}).user is None


def test_expired_session(store):
    auth = _auth(store)
    cookie = auth.create_session(store.by_username("anna"), now=0)
    assert auth.read_session(cookie) is None


def test_ha_ingress_only_from_trusted_proxy(store):
    auth = _auth(store)
    headers = {"X-Remote-User-Id": "ha-42", "X-Remote-User-Name": "luca", "X-Remote-User-Display-Name": "Luca"}
    assert auth.resolve(headers, "192.168.1.50", {}).user is None
    result = auth.resolve(headers, "172.30.32.2", {})
    assert result.user.first_name == "Luca" and result.user.origin == "homeassistant"
    # On the second access it is the same, persisted user
    assert auth.resolve(headers, "172.30.32.2", {}).user.id == result.user.id
    assert UserStore(store.path).by_ha_id("ha-42") is not None


def test_ha_ingress_disabled(store):
    auth = _auth(store, providers=("local",))
    assert auth.resolve({"X-Remote-User-Id": "ha-42"}, "172.30.32.2", {}).user is None


def test_api_token(store):
    auth = _auth(store)
    assert auth.resolve({"Authorization": "Bearer wrong", "X-Factotum-User": "anna"}, None, {}).user is None
    assert auth.resolve({"Authorization": "Bearer tok123"}, None, {}).http_status == 400
    result = auth.resolve({"Authorization": "Bearer tok123", "X-Factotum-User": "anna"}, None, {})
    assert result.user.username == "anna" and result.provider == "api_token"


def test_profiles_are_isolated(store):
    luca = store.from_home_assistant("ha-1", "luca", "Luca")
    store.update_profile(luca, {"calendars": [{"id": "luca@example.com", "label": "Work"}]})
    assert store.by_username("anna").calendars == []
    assert store.get(luca.id).calendars[0].id == "luca@example.com"
    with pytest.raises(ValueError):
        store.update_profile(luca, {"timezone": "Mars/Base"})


def test_contact_details(store):
    anna = store.by_username("anna")
    store.update_profile(anna, {"first_name": "Anna", "last_name": "Bianchi",
                                "email": "anna@example.com", "phone": "+39 333 1234567"})
    reloaded = UserStore(store.path).by_username("anna")
    assert reloaded.name == "Anna Bianchi"
    assert reloaded.email == "anna@example.com" and reloaded.phone == "+39 333 1234567"
    assert "password_hash" not in reloaded.public() and reloaded.public()["name"] == "Anna Bianchi"
    with pytest.raises(ValueError):
        store.update_profile(anna, {"email": "not-an-email"})
    with pytest.raises(ValueError):
        store.update_profile(anna, {"phone": "call me"})
