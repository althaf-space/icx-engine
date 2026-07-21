"""Deterministic UI census -> UiStep flow conversion."""
from __future__ import annotations

from icx_engine.testing.analyzers.to_flow import census_to_flow


def _team_census():
    return {
        "screenName": "Team",
        "functionalitySummaryTable": [
            {"id": "F1", "type": "List"}, {"id": "F2", "type": "Search"},
            {"id": "F5", "type": "Refresh"}, {"id": "F6", "type": "Create"},
            {"id": "F7", "type": "View"}, {"id": "F8", "type": "Edit/Modify"},
        ],
        "functionalities": [
            {"id": "F1", "functionality": "List Teams"},
            {"id": "F2", "functionality": "Search",
             "modalDetails": {"triggerSelector": "#TeamSearchId"}},
            {"id": "F5", "functionality": "Refresh",
             "modalDetails": {"triggerSelector": "[data-testid='team-refresh']"}},
            {"id": "F6", "functionality": "Create Team",
             "modalDetails": {"triggerSelector": "[data-testid='team-create']",
                              "modalSelector": "[data-testid='team-modal']", "modalName": "Create Team"},
             "submitButton": {"selectors": ["[data-testid='team-save']"]},
             "cancelButton": {"selectors": ["[data-testid='team-cancel']"]},
             "fields": [
                 {"label": "Logo URL", "domSelectors": ["[data-testid='team-logo-url']"], "interactionPattern": "default"},
                 {"label": "Team Name EN", "domSelectors": ["[data-testid='team-name-EN']"]},
                 {"label": "Tenant", "domSelectors": ["#tenant"], "interactionPattern": "select",
                  "dropdownOptions": ["SMART", "ADCB"]},
             ],
             "notifications": {"messageSelector": ".notification-warning",
                               "messages": [{"text": "Please fill highlighted fields", "type": "warning"}]}},
            {"id": "F7", "functionality": "View Team",
             "modalDetails": {"triggerSelector": "[data-testid^='team-view-']",
                              "modalSelector": "[data-testid='team-modal']", "modalName": "Team Details"},
             "cancelButton": {"selectors": ["[data-testid='team-cancel']"]}},
            {"id": "F8", "functionality": "Edit Team",
             "modalDetails": {"triggerSelector": "[data-testid^='team-edit-']",
                              "modalSelector": "[data-testid='team-modal']", "modalName": "Modify Team"},
             "cancelButton": {"selectors": ["[data-testid='team-cancel']"]}},
        ],
        "validationMatrix": [{"fieldName": "teamName", "validationType": "mandatory", "errorMessage": "Mandatory."}],
    }


def _descs(steps):
    return [s["description"] for s in steps]


def test_flow_starts_with_goto_and_wait():
    f = census_to_flow(_team_census(), "http://x/#/team")
    assert f[0]["action"] == "goto" and f[0]["target"] == "http://x/#/team"
    # authed proof = waitfor a post-login anchor (a control from the census), not a racy body assert
    assert f[1]["action"] == "waitfor" and f[1]["target"]


def test_flow_covers_search_create_view_edit():
    f = census_to_flow(_team_census(), "http://x/#/team")
    targets = [s["target"] for s in f]
    # search
    assert "#TeamSearchId" in targets
    # refresh
    assert "[data-testid='team-refresh']" in targets
    # create open + modal
    assert "[data-testid='team-create']" in targets
    assert any(s["action"] == "waitfor" and s["target"] == "[data-testid='team-modal']" for s in f)
    # view + edit headers asserted
    assert any(s["action"] == "assert" and s.get("value") == "Team Details" for s in f)
    assert any(s["action"] == "assert" and s.get("value") == "Modify Team" for s in f)


def test_create_has_negative_validation_then_fills_then_submit():
    f = census_to_flow(_team_census(), "http://x/#/team", test_writes=True)
    # NEGATIVE: submit empty -> assert the warning text
    neg = [i for i, s in enumerate(f) if s["action"] == "click" and s["target"] == "[data-testid='team-save']"]
    assert neg, "no team-save click emitted"
    assert any(s["action"] == "assert" and "Please fill highlighted fields" in s.get("value", "") for s in f)
    # every field filled/selected
    assert any(s["action"] == "fill" and s["target"] == "[data-testid='team-logo-url']" for s in f)
    assert any(s["action"] == "select" and s["target"] == "#tenant" and s["value"] == "SMART" for s in f)
    # real write (submit valid) present when test_writes on -> team-save clicked at least twice
    assert len(neg) >= 2


def test_test_writes_off_cancels_instead_of_saving():
    f = census_to_flow(_team_census(), "http://x/#/team", test_writes=False)
    # with writes off there is NO valid-form submit (real write); the form is cancelled instead.
    valid_submit = [s for s in f if s["action"] == "click"
                    and "submit valid form" in s.get("description", "")]
    assert valid_submit == []
    assert any(s["target"] == "[data-testid='team-cancel']" for s in f)


def test_ordering_create_first_then_row_ops():
    # CREATE FIRST (seeds a row) so VIEW / EDIT / DELETE never fail on an empty list; search stays first.
    f = census_to_flow(_team_census(), "http://x/#/team")
    def idx(sub):
        return next(i for i, s in enumerate(f) if sub in s["target"])
    assert idx("#TeamSearchId") < idx("team-create")             # search before create
    assert idx("team-create") < idx("team-view-")                # create before view
    assert idx("team-view-") < idx("team-edit-")                 # view before edit (edit is row-scoped)


def test_sparse_census_still_runnable():
    f = census_to_flow({"functionalities": []}, "http://x")
    assert f and f[0]["action"] == "goto"
    assert census_to_flow(None, "http://x")[0]["action"] == "goto"


def test_all_interaction_patterns_in_create_flow():
    # to_flow must emit the correct action for EVERY 2026 control type.
    fields = [
        {"label": "Name", "domSelectors": ["#name"]},                                  # text -> fill
        {"label": "Country", "domSelectors": ["#country"], "interactionPattern": "select", "dropdownOptions": ["US"]},
        {"label": "Tags", "domSelectors": ["#tags"], "interactionPattern": "multiselect", "dropdownOptions": ["A", "B"]},
        {"label": "Skills", "domSelectors": ["#skills"], "interactionPattern": "tags"},        # tags -> fill+press
        {"label": "City", "domSelectors": ["#city"], "interactionPattern": "combobox"},        # combobox -> fill+press
        {"label": "Agree", "domSelectors": ["#ok"], "interactionPattern": "checkbox"},
        {"label": "Plan", "domSelectors": ["#plan"], "interactionPattern": "segmented"},       # segmented -> click
        {"label": "Stars", "domSelectors": ["#stars"], "interactionPattern": "rating"},        # rating -> click
        {"label": "Volume", "domSelectors": ["#vol"], "interactionPattern": "slider"},         # slider -> setvalue
        {"label": "Shade", "domSelectors": ["#shade"], "interactionPattern": "color"},         # color -> setvalue
        {"label": "Qty", "domSelectors": ["#qty"], "interactionPattern": "stepper"},           # stepper -> fill
        {"label": "Code", "domSelectors": ["#otp"], "interactionPattern": "otp"},              # otp -> fill
        {"label": "DOB", "domSelectors": ["#dob"], "interactionPattern": "datepicker"},
        {"label": "Bio", "domSelectors": ["#bio"], "interactionPattern": "rich-text"},         # richtext -> type
        {"label": "Phone", "domSelectors": ["#ph"], "interactionPattern": "masked"},           # masked -> fill
        {"label": "Doc", "domSelectors": ["#doc"], "interactionPattern": "file-upload", "testFile": "/tmp/a.pdf"},
        {"label": "Card", "domSelectors": ["#card"], "interactionPattern": "drag", "dropTarget": ["#zone"]},
    ]
    m = {"functionalities": [{"id": "C", "functionality": "Create thing",
         "modalDetails": {"triggerSelector": "#c", "modalSelector": "#m"},
         "submitButton": {"selectors": ["#save"]}, "cancelButton": {"selectors": ["#x"]}, "fields": fields}]}
    f = census_to_flow(m, "http://x", test_writes=False)
    acts = {s["target"]: s["action"] for s in f}
    assert acts["#name"] == "smartfill"                   # unclassified text -> dynamic runtime detect
    assert acts["#country"] == "select"
    assert acts["#tags"] == "multiselect"
    assert acts["#skills"] in ("fill", "press")            # tags = fill + press
    assert acts["#city"] in ("fill", "press")              # combobox = fill + press
    assert acts["#ok"] == "check"
    assert acts["#plan"] == "click"                        # segmented
    assert acts["#stars"] == "click"                       # rating
    assert acts["#vol"] == "setvalue"                      # slider
    assert acts["#shade"] == "setvalue"                    # color
    assert acts["#qty"] == "fill"                          # stepper
    assert acts["#otp"] == "fill"                          # otp
    assert acts["#dob"] == "fill"
    assert acts["#bio"] == "type"                          # rich-text editor
    assert acts["#ph"] == "fill"                           # masked
    assert acts["#doc"] == "upload"
    assert acts["#card"] == "draganddrop"
    # tags/combobox emit a press step to commit/pick
    assert any(s["action"] == "press" and s["target"] == "#skills" for s in f)
    assert any(s["action"] == "press" and s["target"] == "#city" for s in f)


def test_perf_step_woven_per_screen():
    f = census_to_flow(_team_census(), "http://x/#/team")
    perf = [s for s in f if s["action"] == "perf"]
    assert len(perf) == 1 and "PERFORMANCE" in perf[0]["description"]


def test_dup_create_woven_when_duplicate_rule_present():
    m = {"functionalities": [{"id": "C", "functionality": "Create thing",
         "modalDetails": {"triggerSelector": "#c", "modalSelector": "#m", "modalName": "New"},
         "submitButton": {"selectors": ["#save"]}, "cancelButton": {"selectors": ["#x"]},
         "fields": [{"label": "Name", "domSelectors": ["#name"]}],
         "notifications": {"messageSelector": ".warn", "messages": [{"text": "fix", "type": "warning"}]}}],
         "validationMatrix": [{"errorMessage": "fix"},
                              {"validationType": "duplicate_check", "errorMessage": "Already exists"}]}
    f = census_to_flow(m, "http://x", test_writes=True)
    assert any("WORKFLOW(dup)" in s.get("description", "") for s in f)
    assert any(s["action"] == "assert" and s.get("value") == "Already exists" for s in f)


def test_delete_verify_woven_with_create_delete_search():
    m = {"functionalitySummaryTable": [{"id": "S", "type": "Search"}, {"id": "C", "type": "Create"},
                                        {"id": "D", "type": "Delete"}],
         "functionalities": [
             {"id": "S", "functionality": "Search", "modalDetails": {"triggerSelector": "#search"}},
             {"id": "C", "functionality": "Create", "modalDetails": {"triggerSelector": "#c", "modalSelector": "#m"},
              "submitButton": {"selectors": ["#save"]}, "fields": [{"label": "N", "domSelectors": ["#n"]}]},
             {"id": "D", "functionality": "Delete", "modalDetails": {"triggerSelector": "[data-testid^='del-']"},
              "submitButton": {"selectors": ["#confirm"]}}]}
    f = census_to_flow(m, "http://x", test_writes=True)
    assert any("WORKFLOW(delete)" in s.get("description", "") for s in f)
    assert any(s["action"] == "assertgone" for s in f)


def test_error_handling_woven_on_refresh_with_api():
    # a refresh functionality that calls an API gets a network-fault case (route 500 -> assert graceful).
    m = {"functionalities": [{"id": "R", "functionality": "Refresh list",
         "modalDetails": {"triggerSelector": "#refresh"},
         "apiIntegration": {"endpoint": "/api/list"}}]}
    f = census_to_flow(m, "http://x")
    assert any(s["action"] == "route" and s["target"] == "/api/list" for s in f)
    assert any(s["action"] == "unroute" for s in f)
    assert any("ERROR-HANDLING" in s.get("description", "") for s in f)


def test_a11y_audit_woven_after_screen_render():
    # every flow must include one accessibility audit right after the screen renders.
    f = census_to_flow(_team_census(), "http://x/#/team")
    a11y = [i for i, s in enumerate(f) if s["action"] == "a11y"]
    assert len(a11y) == 1
    # it runs early (after goto + waitfor), before the functionality scenarios
    assert a11y[0] <= 3
    assert "ACCESSIBILITY" in f[a11y[0]]["description"]


def test_constraint_cases_woven_into_create():
    # every code-inferable constraint (here maxLength + a phone/msisdn format) -> submit-free checks.
    m = {"functionalities": [{"id": "C", "functionality": "Create thing",
         "modalDetails": {"triggerSelector": "#c", "modalSelector": "#m", "modalName": "New"},
         "submitButton": {"selectors": ["#save"]}, "cancelButton": {"selectors": ["#x"]},
         "fields": [{"label": "Code", "domSelectors": ["#code"], "validations": {"maxLength": 8}},
                    {"label": "MSISDN", "domSelectors": ["#msisdn"]}],
         "notifications": {"messageSelector": ".warn", "messages": [{"text": "fix fields", "type": "warning"}]}}],
         "validationMatrix": [{"errorMessage": "fix fields"}]}
    f = census_to_flow(m, "http://x", test_writes=True)
    assert any("CONSTRAINT" in s.get("description", "") for s in f)
    # maxLength -> value-cap assertjs
    assert any(s["action"] == "assertjs" and "value.length <= 8" in s.get("target", "") for s in f)
    # msisdn label inferred as phone format -> a checkValidity constraint check
    assert any("phone/msisdn format" in s.get("description", "") for s in f)


def test_edit_is_destructive_e2e_save_and_verify():
    # E2E: edit must TARGET the created record (search _TAG), change it, SAVE a real update, and VERIFY
    # the change is listed. Needs a search functionality to target/verify.
    from icx_engine.testing.analyzers.to_flow import _TAG, _TAG_EDITED
    m = {"functionalitySummaryTable": [{"id": "S", "type": "Search"}, {"id": "C", "type": "Create"},
                                        {"id": "E", "type": "Edit"}],
         "functionalities": [
             {"id": "S", "functionality": "Search", "modalDetails": {"triggerSelector": "#search"}},
             {"id": "C", "functionality": "Create Team", "modalDetails": {"triggerSelector": "#c", "modalSelector": "#m"},
              "submitButton": {"selectors": ["#save"]}, "fields": [{"label": "Name", "domSelectors": ["#name"]}]},
             {"id": "E", "functionality": "Edit Team",
              "modalDetails": {"triggerSelector": "[data-testid^='team-edit-']", "modalSelector": "#m", "modalName": "Modify Team"},
              "submitButton": {"selectors": ["#update"]}, "cancelButton": {"selectors": ["#x"]},
              "fields": [{"label": "Name", "domSelectors": ["#name"], "validations": {"maxLength": 20}}]}],
         "validationMatrix": [{"errorMessage": "fix"}]}
    f = census_to_flow(m, "http://x", test_writes=True)
    descs = " ".join(s.get("description", "") for s in f)
    assert "EDIT: find OUR created record" in descs            # targets OUR created record via search
    # edit opens via a ROW-SCOPED trigger (only our tagged row) - never existing data
    assert any(s["action"] == "click" and 'tr:has-text' in s["target"] and "Open Edit" in s.get("description", "") for s in f)
    assert "header 'Modify Team'" in descs
    assert any(s["action"] in ("fill", "smartfill") and s["value"] == _TAG_EDITED for s in f)  # changed
    assert "EDIT: SAVE the update (real write)" in descs       # a REAL destructive update write
    assert any(s["action"] == "assert" and s.get("value") == _TAG_EDITED for s in f)  # VERIFY it saved
    # create still verifies its own save
    assert any("CREATE: SAVE the new record" in s.get("description", "") for s in f)
    assert any(s["action"] == "assert" and s.get("value") == _TAG for s in f)   # VERIFY created listed


def test_edit_reverts_to_original_and_cleanup_deletes():
    # an edit must REVERT the record to its original value (no lasting change), and the created record
    # must be DELETED at the end (cleanup - no lasting record).
    from icx_engine.testing.analyzers.to_flow import _TAG, _TAG_EDITED
    m = {"functionalitySummaryTable": [{"id": "S", "type": "Search"}, {"id": "C", "type": "Create"},
                                        {"id": "E", "type": "Edit"}, {"id": "D", "type": "Delete"}],
         "functionalities": [
             {"id": "S", "functionality": "Search", "modalDetails": {"triggerSelector": "#search"}},
             {"id": "C", "functionality": "Create", "modalDetails": {"triggerSelector": "#c", "modalSelector": "#m"},
              "submitButton": {"selectors": ["#save"]}, "fields": [{"label": "Name", "domSelectors": ["#name"]}]},
             {"id": "E", "functionality": "Edit", "modalDetails": {"triggerSelector": "[data-testid^='edit-']", "modalSelector": "#m"},
              "submitButton": {"selectors": ["#update"]}, "fields": [{"label": "Name", "domSelectors": ["#name"]}]},
             {"id": "D", "functionality": "Delete", "modalDetails": {"triggerSelector": "[data-testid^='del-']"},
              "submitButton": {"selectors": ["#confirm"]}}]}
    f = census_to_flow(m, "http://x", test_writes=True)
    descs = " ".join(s.get("description", "") for s in f)
    assert "EDIT: SAVE the update" in descs                         # edit changed it
    assert "REVERT: restore original value" in descs               # then reverted to original
    assert any(s["action"] == "fill" and s["value"] == _TAG and "REVERT" in s.get("description", "") for s in f)
    assert "reverted to original" in descs                         # revert verified
    # cleanup delete targets the ORIGINAL name (edit reverted), and asserts GONE
    assert any(s["action"] == "assertgone" and s["value"] == _TAG for s in f)


def test_download_functionality():
    m = {"functionalitySummaryTable": [{"id": "X", "type": "Download"}],
         "functionalities": [{"id": "X", "functionality": "Export CSV", "modalDetails": {"triggerSelector": "#export"}}]}
    f = census_to_flow(m, "http://x")
    assert any(s["action"] == "download" and s["target"] == "#export" for s in f)


def test_multistep_wizard_create():
    # a create functionality with a `steps` array is navigated step-by-step: fill each step, click
    # NEXT, final submit + verify. Tab-gated steps click their tab first.
    from icx_engine.testing.analyzers.to_flow import _TAG
    m = {"functionalitySummaryTable": [{"id": "S", "type": "Search"}, {"id": "C", "type": "Create"}],
         "functionalities": [
             {"id": "S", "functionality": "Search", "modalDetails": {"triggerSelector": "#q"}},
             {"id": "C", "functionality": "Create User",
              "modalDetails": {"triggerSelector": "#createBtn", "modalSelector": ".modal", "modalName": "User"},
              "submitButton": {"selectors": ["input[value='CREATE']"]}, "cancelButton": {"selectors": ["input[value='CANCEL']"]},
              "steps": [
                  {"name": "Channel", "nextButton": {"selectors": ["input[value='NEXT']"]},
                   "fields": [{"label": "Channel", "domSelectors": ["#channelType"], "interactionPattern": "select"}]},
                  {"name": "Profile", "tabSelector": "a.nav-link", "nextButton": {"selectors": ["input[value='NEXT']"]},
                   "fields": [{"label": "First Name", "domSelectors": ["#firstName"]}]}]}]}
    f = census_to_flow(m, "http://x", test_writes=True)
    descs = " ".join(s.get("description", "") for s in f)
    assert "wizard: go to 'Profile'" in descs           # tab navigation to the gated step
    assert "wizard: NEXT" in descs                       # advance via NEXT
    assert any(s["action"] == "fill" and s["value"] == _TAG for s in f)   # identifying value on its step
    assert "CREATE: SAVE (real write)" in descs          # final submit
    assert any(s["action"] == "assert" and s.get("value") == _TAG for s in f)   # verify listed
    # NEXT count = steps - 1 (last step submits, does not NEXT)
    nexts = [s for s in f if "wizard: NEXT" in s.get("description", "")]
    assert len(nexts) == 1


def test_constraint_source_runtime_uses_fillunique():
    # runtime/both -> non-identifying value fields become `fillunique` (harness reads live constraints);
    # static -> Python-generated value. The identifying field stays static (searchable) in both.
    m = {"functionalitySummaryTable": [{"id": "S", "type": "Search"}, {"id": "C", "type": "Create"}],
         "functionalities": [
             {"id": "S", "functionality": "Search", "modalDetails": {"triggerSelector": "#q"}},
             {"id": "C", "functionality": "Create", "modalDetails": {"triggerSelector": "#c", "modalSelector": "#m"},
              "submitButton": {"selectors": ["#save"]},
              "fields": [{"label": "Name", "domSelectors": ["#name"]},
                         {"label": "MSISDN", "domSelectors": ["#ph"]}]}]}
    static = census_to_flow(m, "http://x", test_writes=True, constraint_source="static")
    runtime = census_to_flow(m, "http://x", test_writes=True, constraint_source="runtime")
    # static: no fillunique
    assert not any(s["action"] == "fillunique" for s in static)
    # runtime: the non-identifying MSISDN field is fillunique carrying its hint + uniq token
    fu = [s for s in runtime if s["action"] == "fillunique" and s["target"] == "#ph"]
    assert fu and fu[0]["value"] == "phone" and "uniq=" in fu[0]["description"]
    # identifying #name stays a static fill (searchable) in BOTH modes
    for flow in (static, runtime):
        assert any(s["action"] == "fill" and s["target"] == "#name" and "identifying" in s["description"] for s in flow)


def test_values_are_unique_and_length_safe():
    # every generated value must (a) never exceed the field maxLength, (b) carry the run's unique token
    # so a uniqueness/duplicate-check field never collides across runs.
    from icx_engine.testing.analyzers.to_flow import _valid_value, _tag_for, _UNIQ
    cases = [
        {"label": "Name", "domSelectors": ["#n"], "validations": {"maxLength": 20}},
        {"label": "Syn", "domSelectors": ["#s"], "validations": {"maxLength": 3}},     # tiny
        {"label": "X", "domSelectors": ["#x"], "validations": {"maxLength": 1}},        # extreme
        {"label": "Email", "type": "email", "domSelectors": ["#e"], "validations": {"maxLength": 15}},
        {"label": "MSISDN", "domSelectors": ["#p"]},
        {"label": "Logo URL", "domSelectors": ["#u"]},
    ]
    tail = _UNIQ[-1]
    for f in cases:
        v = _valid_value(f)
        ml = (f.get("validations") or {}).get("maxLength")
        assert ml is None or len(v) <= ml, f"{f['label']} value {v!r} exceeds maxLength {ml}"
        # some part of the unique token appears (full token, or its tail in a tiny field)
        assert _UNIQ in v or tail in v, f"{f['label']} value {v!r} has no unique token"
    # identifying tag truncation preserves the token even in a 3-char field
    assert _tag_for({"validations": {"maxLength": 3}}, "Test " + _UNIQ) == _UNIQ[-3:]


def test_search_input_gets_xss_and_sqli_security():
    # any input that reaches the backend (search/filter) must get reflected-XSS + SQLi survives checks.
    m = {"functionalitySummaryTable": [{"id": "S", "type": "Search"}],
         "functionalities": [{"id": "S", "functionality": "Search", "modalDetails": {"triggerSelector": "#q"}}]}
    f = census_to_flow(m, "http://x")
    descs = " ".join(s.get("description", "") for s in f)
    assert "SECURITY(XSS): reflected" in descs
    assert "SECURITY(SQLi)" in descs
    assert any(s["action"] == "assertjs" for s in f)   # XSS no-exec asserted


def test_view_stays_read_only():
    # view must NOT fill/submit - open, assert header, close only.
    m = {"functionalitySummaryTable": [{"id": "V", "type": "View"}],
         "functionalities": [{"id": "V", "functionality": "View Team",
             "modalDetails": {"triggerSelector": "[data-testid^='team-view-']", "modalSelector": "#m",
                              "modalName": "Team Details"},
             "cancelButton": {"selectors": ["#x"]}}]}
    f = census_to_flow(m, "http://x", test_writes=True)
    view_region = [s for s in f if "View Team" in s.get("description", "") or s["target"] == "#x"]
    assert not any(s["action"] in ("fill", "assertjs") for s in view_region)   # no writes in view


def test_security_xss_woven_into_create():
    # every create flow must include the XSS security case (always-on), injecting the canary + assertjs.
    from icx_engine.testing.analyzers.security_cases import XSS_PAYLOAD, XSS_SAFE_EXPR
    f = census_to_flow(_team_census(), "http://x/#/team", test_writes=True)
    assert any(s["action"] == "assertjs" and s["target"] == XSS_SAFE_EXPR for s in f)
    assert any(s["action"] == "fill" and s["value"] == XSS_PAYLOAD for s in f)
    assert any("SECURITY(XSS)" in s.get("description", "") for s in f)


def test_field_value_generation():
    m = {"functionalities": [{"id": "C", "functionality": "Create",
         "modalDetails": {"triggerSelector": "#c", "modalSelector": "#m"},
         "submitButton": {"selectors": ["#save"]},
         "fields": [{"label": "Email", "domSelectors": ["#email"]},
                    {"label": "Count", "domSelectors": ["#count"]},
                    {"label": "Photo URL", "domSelectors": ["#url"]}]}]}
    f = census_to_flow(m, "http://x", test_writes=False)
    vals = {s["target"]: s["value"] for s in f if s["action"] in ("fill", "smartfill")}
    assert "@" in vals["#email"]
    assert vals["#count"] == "10"
    assert vals["#url"].startswith("https://")


# -- non-CRUD archetype coverage: dashboards (render) + reports (generate) ------

def test_render_kind_asserts_every_widget():
    # a dashboard with no CRUD: discovery emits a Render functionality carrying the widgets it found;
    # the flow must assert each one renders (this is the coverage a dashboard used to get NONE of).
    m = {"functionalities": [{"id": "F_RENDER", "functionality": "Render", "type": "Render",
         "modalDetails": {"triggerSelector": "svg.recharts-surface"},
         "widgets": [{"kind": "chart", "selector": "svg.recharts-surface"},
                     {"kind": "grid", "selector": "table.data"},
                     {"kind": "card", "selector": ".kpi-box"}]}]}
    f = census_to_flow(m, "http://x/#/dash", test_writes=True)
    # the FIRST widget is a hard VISIBLE waitfor (proves the screen painted); the rest wait for ATTACHED
    # (in DOM) - tolerating an async-drawn chart or a zero-height no-data chart, still failing a widget
    # that never renders.
    vis = [s["target"] for s in f if s["action"] == "waitfor" and s.get("value") != "attached" and "RENDER" in s.get("description", "")]
    assert "svg.recharts-surface" in vis
    built = [s["target"] for s in f if s["action"] == "waitfor" and s.get("value") == "attached" and "built" in s.get("description", "")]
    assert "table.data" in built and ".kpi-box" in built
    # the chart gets a SOFT "drew data" probe (an empty-data chart must skip, not fail)
    chart = [s for s in f if s["action"] == "assertjs" and "drew" in s.get("description", "")]
    assert chart and chart[0].get("soft") is True
    # graceful no-blank/no-crash check, also soft
    assert any(s["action"] == "assertjs" and "not blank" in s.get("description", "") and s.get("soft") for s in f)


def test_report_kind_fills_filters_generates_and_checks_result():
    m = {"functionalities": [{"id": "F_REPORT", "functionality": "Generate Report", "type": "Report",
         "fields": [{"label": "From", "domSelectors": ["#from"], "type": "date"},
                    {"label": "Segment", "domSelectors": ["#seg"], "interactionPattern": "select"}],
         "submitButton": {"selectors": ["button:has-text('Generate')"]},
         "resultSelector": "table.result"}]}
    f = census_to_flow(m, "http://x/#/rep", test_writes=True)
    # both filters are filled, the generate button clicked, and the result region waited for
    assert any(s["action"] in ("fill", "select", "smartfill") and s["target"] == "#from" for s in f)
    assert any(s["action"] in ("select", "smartfill", "pickoption") and s["target"] == "#seg" for s in f)
    assert any(s["action"] == "click" and s["target"] == "button:has-text('Generate')" for s in f)
    assert any(s["action"] == "waitfor" and s["target"] == "table.result" for s in f)


def test_render_report_do_not_break_crud_tail():
    # a screen carrying BOTH a dashboard render AND a normal create still produces the CRUD lifecycle.
    m = {"functionalities": [
        {"id": "F_RENDER", "functionality": "Render", "type": "Render",
         "modalDetails": {"triggerSelector": ".chart"}, "widgets": [{"kind": "chart", "selector": ".chart"}]},
        {"id": "C", "functionality": "Create", "modalDetails": {"triggerSelector": "#c", "modalSelector": "#m"},
         "submitButton": {"selectors": ["#save"]}, "fields": [{"label": "Name", "domSelectors": ["#n"]}]}]}
    f = census_to_flow(m, "http://x", test_writes=True)
    assert any("RENDER" in s.get("description", "") for s in f)
    assert any(s["action"] == "click" and s["target"] == "#save" for s in f)


def test_uistep_soft_flag_survives_flow_roundtrip():
    # regression: the deterministic path drops unknown keys; soft MUST survive census->UiStep->disk->harness
    from icx_engine.testing.runners.ui import UiStep, UiFlow
    flow = UiFlow("t", "u", steps=[UiStep("assertjs", "expr", "", "d", soft=True),
                                    UiStep("waitfor", "#x", "", "hard")])
    rt = UiFlow.from_dict(flow.to_dict())
    assert rt.steps[0].soft is True and rt.steps[1].soft is False


def test_render_skips_malformed_widget_selector():
    # a crawler bug can serialize an SVG className as "[object SVGAnimatedString]" -> "svg.[object...]";
    # such a selector throws in the browser and poisons the anchor. to_flow must filter it out.
    from icx_engine.testing.analyzers.to_flow import _valid_css
    assert _valid_css("svg.recharts-surface") is True
    assert _valid_css("#chart") is True
    assert _valid_css("svg.[object.SVGAnimatedString]") is False
    assert _valid_css("g.[object.SVGAnimatedString]") is False
    assert _valid_css("div[class='x'") is False           # unbalanced
    m = {"functionalities": [{"id": "F_RENDER", "functionality": "Render", "type": "Render",
         "modalDetails": {"triggerSelector": "svg.[object.SVGAnimatedString]"},
         "widgets": [{"kind": "chart", "selector": "svg.[object.SVGAnimatedString]"},
                     {"kind": "grid", "selector": "table.data"}]}]}
    f = census_to_flow(m, "http://x/#/d", test_writes=True)
    targets = [s["target"] for s in f]
    assert "svg.[object.SVGAnimatedString]" not in targets   # malformed dropped everywhere
    assert any(s["action"] == "waitfor" and s["target"] == "table.data" for s in f)  # good one kept
    # anchor did not fall to the malformed selector (it would poison the render waitfor)
    anchor_wait = next(s for s in f if "authenticated screen" in s.get("description", ""))
    assert anchor_wait["target"] != "svg.[object.SVGAnimatedString]"


def test_report_kind_without_apply_button_still_exercises_filters():
    # a dashboard with page filters but NO generate button (auto-reloads): the report kind must still
    # set the filters and check the screen stays healthy - not skip coverage for lack of a submit.
    m = {"functionalitySummaryTable": [{"id": "F_REPORT", "type": "Report"}],
         "functionalities": [{"id": "F_REPORT", "functionality": "Filter Data",
         "fields": [{"label": "From", "domSelectors": ["#from"], "type": "date"},
                    {"label": "To", "domSelectors": ["#to"], "type": "date"}],
         "submitButton": {"selectors": []}, "resultSelector": ""}]}
    f = census_to_flow(m, "http://x/#/g", test_writes=True)
    assert any(s["action"] in ("fill", "smartfill") and s["target"] == "#from" for s in f)
    assert any(s["action"] in ("fill", "smartfill") and s["target"] == "#to" for s in f)
    # no generate button -> no click on a submit, but the graceful no-error check still runs
    assert any(s["action"] == "assertjs" and "no error" in s.get("description", "") and s.get("soft") for s in f)


def test_bespoke_create_no_fields_degrades_edit_and_delete_to_safe():
    # a bespoke create (dual-list privilege matrix / rule builder) opens a modal but exposes NO fillable
    # form. It cannot produce a record, so: create is structural (open/close, no save), edit is
    # structural (open/assert/close - NOT a tag hunt for a record never created), and the destructive
    # delete-verify is SKIPPED entirely (data safety - never delete existing data we did not create).
    m = {"functionalitySummaryTable": [{"id": "S", "type": "Search"}, {"id": "C", "type": "Create"},
                                        {"id": "E", "type": "Edit"}, {"id": "D", "type": "Delete"}],
         "functionalities": [
             {"id": "S", "functionality": "Search", "modalDetails": {"triggerSelector": "#search"}},
             {"id": "C", "functionality": "Create Privilege",
              "modalDetails": {"triggerSelector": "#c", "modalSelector": "#m", "modalName": "Create Privilege"},
              "submitButton": {"selectors": []}, "fields": []},                      # NO fillable form
             {"id": "E", "functionality": "Edit", "modalDetails": {"triggerSelector": "img[title='Modify']", "modalSelector": "#m", "modalName": "Edit"}},
             {"id": "D", "functionality": "Delete", "modalDetails": {"triggerSelector": "img[title='Delete']"},
              "submitButton": {"selectors": ["#confirm"]}}]}
    f = census_to_flow(m, "http://x", test_writes=True)
    descs = " ".join(s.get("description", "") for s in f)
    assert "CREATE: SAVE" not in descs                       # nothing to save -> no false save/verify
    assert "find OUR created record" not in descs            # edit does not hunt a never-created record
    assert "WORKFLOW(delete)" not in descs                   # destructive delete skipped (data safety)
    assert "trigger delete" not in descs                     # plain non-scoped delete never emitted
    assert any(s["action"] == "click" and s["target"] == "img[title='Modify']" for s in f)  # edit opens structurally
    assert any(s["action"] == "assert" and "Edit" in s.get("value", "") for s in f)          # asserts its header


def test_writable_create_still_gets_full_lifecycle():
    # regression guard: a NORMAL create (real fields + submit) keeps the full write lifecycle + row-scoped delete.
    m = {"functionalitySummaryTable": [{"id": "S", "type": "Search"}, {"id": "C", "type": "Create"},
                                        {"id": "D", "type": "Delete"}],
         "functionalities": [
             {"id": "S", "functionality": "Search", "modalDetails": {"triggerSelector": "#search"}},
             {"id": "C", "functionality": "Create", "modalDetails": {"triggerSelector": "#c", "modalSelector": "#m"},
              "submitButton": {"selectors": ["#save"]}, "fields": [{"label": "Name", "domSelectors": ["#n"]}]},
             {"id": "D", "functionality": "Delete", "modalDetails": {"triggerSelector": "[data-testid^='del-']"},
              "submitButton": {"selectors": ["#confirm"]}}]}
    f = census_to_flow(m, "http://x", test_writes=True)
    descs = " ".join(s.get("description", "") for s in f)
    assert "CREATE: SAVE" in descs and "WORKFLOW(delete)" in descs
