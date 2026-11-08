from html.parser import HTMLParser
from pathlib import Path
import re


DASHBOARD_PATH = Path(__file__).resolve().parents[1] / "dashboard_ui.html"


class DashboardParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.nav_hrefs = []
        self.option_values = set()
        self.history_periods = set()
        self.text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "a" and attrs.get("href", "").startswith("#"):
            self.nav_hrefs.append(attrs["href"][1:])
        if tag == "option" and "value" in attrs:
            self.option_values.add(attrs["value"])
        if "data-period" in attrs:
            self.history_periods.add(attrs["data-period"])

    def handle_data(self, data):
        self.text.append(data)


def dashboard_source():
    return DASHBOARD_PATH.read_text(encoding="utf-8")


def parse_dashboard():
    source = dashboard_source()
    parser = DashboardParser()
    parser.feed(source)
    parser.close()
    return source, parser


def test_mobile_navigation_uses_an_accessible_drawer_not_a_sticky_bar():
    source, page = parse_dashboard()
    css = source.split("<style>", 1)[1].split("</style>", 1)[0]

    assert {"navToggle", "dashboardNav", "railBackdrop"} <= set(page.ids)
    assert 'aria-controls="dashboardNav"' in source
    mobile_rules = css.split("@media(max-width:900px)", 1)[1].split("@media(max-width:760px)", 1)[0]
    assert ".rail{position:fixed" in mobile_rules
    assert ".rail.is-open" in mobile_rules
    assert ".rail{position:sticky" not in mobile_rules


def test_mobile_navigation_closes_on_escape_and_section_selection():
    source, _ = parse_dashboard()

    assert 'event.key === "Escape"' in source
    assert 'dashboardNav.querySelectorAll("a[href^=\'#\']")' in source
    assert 'setNavigationOpen(false, false)' in source


def test_liveness_from_overview_updates_header_and_duplicate_alerts():
    source, _ = parse_dashboard()
    overview = source.split("function applyOverview(d)", 1)[1].split("function applySlow(d)", 1)[0]

    assert 'd.liveness && !d.liveness.error' in overview
    assert 'S.liveness = d.liveness' in overview


def test_websocket_failure_falls_back_to_http_refresh():
    source, _ = parse_dashboard()
    fallback = source.split("function startPolling()", 1)[1].split("function resolveAutoTimeframe()", 1)[0]

    assert 'getJSON("/api/overview", generation)' in fallback
    assert 'getJSON("/api/workflow", generation)' in fallback
    assert "setInterval(refresh, 5000)" in fallback
    assert "Dashboard connection lost" in fallback


def test_navigation_links_resolve_to_unique_section_ids():
    _, page = parse_dashboard()

    assert page.nav_hrefs, "The dashboard should have in-page navigation links."
    assert len(page.nav_hrefs) == len(set(page.nav_hrefs))
    assert all(page.ids.count(target) == 1 for target in page.nav_hrefs)


def test_existing_dashboard_render_contract_ids_remain():
    _, page = parse_dashboard()
    expected_ids = {
        "alerts", "kpis", "kEquity", "kPnl", "kDd", "kPos", "posBody",
        "scanBody", "tradeBody", "portfolioChart", "symChart", "rChart",
        "journalStats", "optGrid", "logs", "footTs",
    }

    assert expected_ids <= set(page.ids)


def test_risk_first_sections_precede_secondary_analytics():
    _, page = parse_dashboard()
    targets = ["overview", "positionsSection", "activitySection", "scanSection", "diagnosticsSection"]
    positions = [page.ids.index(target) for target in targets]

    assert positions == sorted(positions)


def test_read_only_and_existing_theme_controls_remain():
    source, page = parse_dashboard()
    visible_text = " ".join(page.text)

    assert "Read-only dashboard" in visible_text
    assert "no orders can be placed" in visible_text
    assert {"cyberpunk", "oled", "navy", "matrix"} <= page.option_values
    assert 'localStorage.getItem("dashboard-theme")' in source


def test_kpi_metric_labels_use_sentence_case():
    source, _ = parse_dashboard()
    css = source.split("<style>", 1)[1].split("</style>", 1)[0]
    rules = re.findall(r"\.kpi\s+\.label\s*\{([^}]*)\}", css)
    declarations = {}
    for rule in rules:
        for declaration in rule.split(";"):
            if ":" in declaration:
                name, value = declaration.split(":", 1)
                declarations[name.strip()] = value.strip()

    assert declarations.get("text-transform") == "none"


def test_guardrails_distinguish_unavailable_from_safe_positions():
    source, _ = parse_dashboard()
    guardrails = source.split("function renderGuardrails()", 1)[1].split("function renderAlerts()", 1)[0]

    assert "S.positionsError" in guardrails
    assert "Position capacity unavailable" in guardrails
    assert "Protection status unavailable" in guardrails


def test_slow_liveness_updates_refresh_guardrails():
    source, _ = parse_dashboard()
    apply_slow = source.split("function applySlow(d)", 1)[1].split("/* ---------- WebSocket", 1)[0]

    assert "renderGuardrails()" in apply_slow


def test_account_error_does_not_leave_old_kpis_looking_current():
    source, _ = parse_dashboard()
    render_kpis = source.split("function renderKpis()", 1)[1].split("function renderPositions()", 1)[0]

    assert "S.accountError" in render_kpis
    assert "Account snapshot unavailable" in render_kpis


def test_position_error_does_not_leave_old_rows_looking_current():
    source, _ = parse_dashboard()
    render_positions = source.split("function renderPositions()", 1)[1].split("function renderScan()", 1)[0]

    assert "S.positionsError" in render_positions
    assert "Position data unavailable" in render_positions


def test_account_selector_has_both_profiles_and_persists_only_the_alias():
    source, page = parse_dashboard()

    assert "profileSelect" in page.ids
    assert {"primary", "secondary"} <= page.option_values
    assert 'localStorage.setItem("dashboard-account-profile"' in source


def test_profile_switch_routes_requests_and_rejects_stale_responses():
    source, _ = parse_dashboard()

    assert "function apiUrl(path)" in source
    assert "fetch(apiUrl(url)" in source
    assert "new WebSocket(websocketUrl())" in source
    assert "generation !== S.profileGeneration" in source
    assert "profile is unavailable" in source


def test_profile_switch_clears_account_scoped_state():
    source, _ = parse_dashboard()
    switch = source.split("function switchProfile(", 1)[1].split("function ", 1)[0]
    clear = source.split("function clearProfileState()", 1)[1].split("function ", 1)[0]

    assert "clearProfileState()" in switch
    assert "S.overview = S.account = S.positions = S.attention = null" in clear
    assert "S.portfolioHistory = null" in clear
    assert "S.logCursor = 0" in clear
    assert "S.logRows = []" in clear


def test_account_selector_does_not_expose_credentials_or_add_trading_controls():
    source, _ = parse_dashboard()

    assert "ALPACA_API_KEY" not in source
    assert "ALPACA_SECRET_KEY" not in source
    assert "account_number" not in source
    assert "place order" not in source.lower()
    assert "start bot" not in source.lower()
    assert "stop bot" not in source.lower()


def test_portfolio_chart_has_range_resolution_and_metric_controls():
    source, page = parse_dashboard()

    assert {"portfolioChart", "portfolioHistoryControls", "historyResolution", "historyTable", "historyTableToggle"} <= set(page.ids)
    assert {"1D", "1W", "1M", "3M", "6M", "1Y", "All"} <= page.history_periods
    assert {"Auto", "1H", "1D"} <= page.option_values
    assert 'data-metric="equity"' in source
    assert 'data-metric="pnl_pct"' in source


def test_portfolio_chart_uses_profile_routed_getjson_and_generation_guard():
    source, _ = parse_dashboard()

    assert "async function loadPortfolioHistory(" in source
    assert "getJSON(url, generation)" in source
    assert "generation !== S.profileGeneration" in source
    switch = source.split("function switchProfile(", 1)[1].split("function ", 1)[0]
    assert "loadPortfolioHistory()" in switch


def test_portfolio_chart_formats_fractional_alpaca_pnl_percent():
    source, _ = parse_dashboard()

    assert "profit_loss_pct" in source
    assert "* 100" in source


def test_portfolio_chart_shows_unavailable_and_table_states():
    source, _ = parse_dashboard()

    assert "Portfolio history unavailable for the local simulator" in source
    assert "historyTable" in source
    assert "No Alpaca portfolio-history points" in source
