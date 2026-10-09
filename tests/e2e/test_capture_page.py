import pytest

from tests.e2e.conftest import TOKEN


def open_capture(page, base_url, width=390, height=844):
    prompts = []

    def handle_dialog(dialog):
        if dialog.type == "prompt":
            prompts.append(dialog.message)
            dialog.accept(TOKEN)
        else:
            dialog.accept()

    page.set_viewport_size({"width": width, "height": height})
    page.on("dialog", handle_dialog)
    page.goto(base_url)
    page.locator("#capture-privacy").wait_for()
    return prompts


@pytest.mark.e2e
@pytest.mark.parametrize(("width", "height"), [(390, 844), (1280, 800)])
def test_e0_capture_shell_and_auth_smoke(page, ui_server, width, height):
    base_url = ui_server()
    prompts = open_capture(page, base_url, width, height)

    assert page.get_by_role("heading", name="Capture").first.is_visible()
    assert page.locator("#capture-privacy").inner_text().startswith("AI: 127.0.0.1 · local")
    assert page.get_by_label("What would you like to remember?").is_visible()
    assert page.evaluate("localStorage.getItem('hub_token')") == TOKEN
    assert len(prompts) == 1


@pytest.mark.e2e
def test_e1_g1_plan_renders_read_only(page, ui_server):
    base_url = ui_server()
    open_capture(page, base_url)
    page.get_by_label("What would you like to remember?").fill(
        "Working with Matt on the PMT tool since June, launch is Oct 13."
    )
    page.get_by_role("button", name="Analyze").click()

    page.locator("#capture-plan .capture-op").nth(2).wait_for()
    assert page.locator("#capture-plan .capture-op").count() == 3
    assert page.locator("#capture-plan").get_by_text("PMT launch").count() >= 1
    assert page.locator("#capture-plan input[type=checkbox]").count() == 0
    assert page.get_by_role("button", name="Apply").count() == 0


@pytest.mark.e2e
def test_e1_failed_plan_can_be_saved_as_note(page, ui_server):
    base_url = ui_server()
    open_capture(page, base_url)
    page.get_by_label("What would you like to remember?").fill("invalid-output sample")
    page.get_by_role("button", name="Analyze").click()

    page.locator("#capture-state").get_by_text("invalid plan").wait_for()
    page.get_by_role("button", name="Save as note instead").click()
    page.get_by_role("link", name="Open note").wait_for()


@pytest.mark.e2e
def test_e1_secret_gate_requires_explicit_override(page, ui_server):
    base_url = ui_server()
    open_capture(page, base_url)
    page.get_by_label("What would you like to remember?").fill("password: hunter2!")
    page.get_by_role("button", name="Analyze").click()

    page.get_by_role("alert").get_by_text("Possible secret detected").wait_for()
    page.get_by_role("button", name="Send anyway").click()
    page.locator("#capture-plan .capture-op").nth(2).wait_for()


@pytest.mark.e2e
def test_e1_long_text_can_be_saved_without_capture(page, ui_server):
    base_url = ui_server(capture_max_chars=16)
    open_capture(page, base_url)
    text = "x" * 17
    page.get_by_label("What would you like to remember?").evaluate(
        "(element, value) => { element.value = value; element.dispatchEvent(new Event('input', {bubbles: true})); }",
        text,
    )

    page.get_by_role("button", name="Analyze").click()
    page.get_by_text("Too long for AI capture").wait_for()
    page.get_by_role("button", name="Save as note instead").click()
    page.get_by_role("link", name="Open note").wait_for()


@pytest.mark.e2e
def test_e1_duplicate_and_daily_limit_messages(page, ui_server):
    base_url = ui_server(capture_daily_call_limit=1)
    open_capture(page, base_url)
    sample = "Working with Matt on the PMT tool since June, launch is Oct 13."
    page.get_by_label("What would you like to remember?").fill(sample)
    page.get_by_role("button", name="Analyze").click()
    page.locator("#capture-plan .capture-op").nth(2).wait_for()
    capture_id = page.evaluate("location.hash.split('/')[1]")
    response = page.evaluate(
        """async (id) => {
          const token = localStorage.getItem("hub_token");
          const response = await fetch(`/api/capture/${id}/apply`, {
            method: "POST",
            headers: {"Authorization": "Bearer " + token,
                      "Content-Type": "application/json"},
            body: "{}"
          });
          return response.status;
        }""",
        capture_id,
    )
    assert response == 200

    page.get_by_role("button", name="Capture").click()
    page.get_by_label("What would you like to remember?").fill(sample)
    page.get_by_role("button", name="Analyze").click()
    page.get_by_text("This text was applied recently").wait_for()
    page.get_by_label("What would you like to remember?").fill("A different thought to remember.")
    page.get_by_role("button", name="Analyze").click()
    page.get_by_text("Daily AI call limit reached").wait_for()


@pytest.mark.e2e
def test_e1_draft_survives_failure_and_clears_after_create(page, ui_server):
    base_url = ui_server()
    open_capture(page, base_url)
    textarea = page.get_by_label("What would you like to remember?")
    textarea.fill("Draft survives a temporary network failure.")
    page.wait_for_function(
        "() => JSON.parse(localStorage.getItem('pkb.capture.draft') || '{}').text === "
        "'Draft survives a temporary network failure.'"
    )
    page.reload()
    textarea = page.get_by_label("What would you like to remember?")
    assert textarea.input_value() == "Draft survives a temporary network failure."

    def fail_capture(route):
        if route.request.method == "POST" and route.request.url.rstrip("/").endswith("/api/capture"):
            route.abort()
        else:
            route.continue_()

    page.route("**/api/capture**", fail_capture)
    page.get_by_role("button", name="Analyze").click()
    page.locator("#capture-error").wait_for()
    assert textarea.input_value() == "Draft survives a temporary network failure."
    page.unroute("**/api/capture**", fail_capture)

    page.get_by_role("button", name="Analyze").click()
    page.locator("#capture-plan .capture-op").nth(2).wait_for()
    assert page.evaluate("localStorage.getItem('pkb.capture.draft')") is None
