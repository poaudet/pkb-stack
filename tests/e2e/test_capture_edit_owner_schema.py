import pytest

from tests.e2e.conftest import TOKEN


def open_capture(page, base_url):
    page.on("dialog", lambda dialog: dialog.accept(TOKEN) if dialog.type == "prompt" else dialog.accept())
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(base_url)
    page.locator("#capture-privacy").wait_for()


def submit(page, text):
    page.get_by_label("What would you like to remember?").fill(text)
    page.get_by_role("button", name="Analyze").click()
    page.locator("#capture-plan .capture-op").first.wait_for()


@pytest.mark.e2e
def test_e2_owner_gap_can_be_set_and_reanalyzed(page, ui_server):
    open_capture(page, ui_server())
    submit(page, "I like kiwis a lot.")

    page.get_by_label("Owner name").fill("Taylor")
    page.get_by_role("button", name="Save and re-analyze").click()
    page.locator("#capture-plan .capture-gaps").wait_for(state="detached")
    page.locator("#capture-plan [data-op-id='owner']").wait_for()

    assert page.locator("#capture-plan").get_by_text("Likes kiwis").count() >= 1
    assert page.locator("#capture-plan [data-op-id='owner'] .capture-diff").count() >= 1
    assert page.locator("#capture-owner-detail").get_by_text("Facts about you go to: Taylor").is_visible()


@pytest.mark.e2e
def test_e2_e3_schema_dependency_apply_undo_and_mobile_actions(page, ui_server):
    open_capture(page, ui_server(owner_name="Taylor"))
    submit(page, "My favorite color is blue.")

    owner = page.locator('[data-op-select="owner"]')
    field = page.locator('[data-op-select="field"]')
    owner.wait_for()
    if not owner.is_checked():
        owner.check()
    assert field.is_checked()
    field.uncheck()
    assert not owner.is_checked()
    owner.check()
    assert field.is_checked()

    action_bar = page.locator("#capture-action-bar")
    assert action_bar.is_visible()
    apply_button = page.get_by_role("button", name="Apply selected")
    page.wait_for_function("() => !document.querySelector('#capture-apply').disabled")
    assert apply_button.is_enabled()
    assert apply_button.evaluate("(button) => button.getBoundingClientRect().height") >= 44

    await_last = page.locator("#capture-plan .capture-op").last
    await_last.scroll_into_view_if_needed()
    assert await_last.evaluate(
        "(card) => card.getBoundingClientRect().bottom <= "
        "document.querySelector('#capture-action-bar').getBoundingClientRect().top + 1"
    )

    apply_button.click()
    page.get_by_role("dialog").get_by_role("button", name="Add field(s)").click()
    page.get_by_role("heading", name="Capture results").wait_for()
    page.locator('a[href="#types/person"]').click()
    details = page.locator("#e-type-details")
    details.get_by_text("Type details · v2").wait_for()
    details.locator("summary").click()
    details.get_by_text("favorite_color", exact=False).wait_for()

    page.get_by_role("button", name="Capture").click()
    page.locator("#capture-history a[href^='#capture/']").first.click()
    page.get_by_role("button", name="Undo").click()
    page.get_by_role("dialog").get_by_role("button", name="Undo").click()
    page.get_by_text("Undo report").wait_for()

    page.get_by_role("button", name="Entities").click()
    page.locator("#e-type").select_option(value="person")
    page.locator("#e-type-details").get_by_text("Type details · v3").wait_for()
    page.locator("#e-type-details summary").click()
    assert page.locator("#e-type-details").get_by_text("favorite_color", exact=False).count() == 0


@pytest.mark.e2e
def test_e3_type_add_field_and_protected_owner_confirmation(page, ui_server):
    open_capture(page, ui_server(owner_name="Taylor"))
    page.get_by_role("button", name="Entities").click()
    page.locator("#e-type").select_option(value="person")
    page.locator("#e-type-details summary").click()
    page.get_by_role("button", name="Add field").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Field name").fill("nickname")
    dialog.get_by_label("Field type").select_option("text")
    dialog.get_by_label("Field description").fill("A familiar name.")
    dialog.get_by_role("button", name="Add field").click()
    page.locator("#e-type-details").get_by_text("Type details · v2").wait_for()
    page.locator("#e-type-details summary").click()
    page.locator("#e-type-details").get_by_role("button", name="History").click()
    page.locator("#e-type-details").get_by_text("v1", exact=False).wait_for()
    page.locator("#e-type-details").get_by_text("added nickname").wait_for()
    page.locator("#e-table .card").first.click()

    entity_dialog = page.locator("#entity-dialog")
    entity_dialog.get_by_role("button", name="Archive").click()
    confirmation = page.get_by_role("dialog").filter(has=page.get_by_text("Protected owner record"))
    confirmation.get_by_role("button", name="Cancel").click()
    assert entity_dialog.is_visible()
    entity_dialog.get_by_role("button", name="Close").click()
    page.locator("#e-table .card").first.click()
    page.locator("#entity-dialog").get_by_role("button", name="Archive").click()
    page.get_by_role("dialog").filter(has=page.get_by_text("Protected owner record")) \
        .get_by_role("button", name="Force this action").click()
    page.get_by_role("button", name="Capture").click()
    page.locator("#capture-owner").get_by_role("button", name="Set up").wait_for()
    page.locator("#capture-owner").get_by_role("button", name="Set up").click()
    owner_dialog = page.get_by_role("dialog").filter(has=page.get_by_text("Owner record"))
    owner_dialog.get_by_label("Owner name").wait_for()
    owner_dialog.get_by_role("button", name="Close").click()


@pytest.mark.e2e
def test_e3_status_owner_capture_card_and_add_shortcut(page, ui_server):
    open_capture(page, ui_server())
    page.get_by_role("button", name="Status").click()
    page.get_by_text("Model: capture-test").wait_for()
    owner_form = page.locator("#status-owner-form")
    owner_form.get_by_label("Owner name").fill("Taylor")
    owner_form.get_by_role("button", name="Set owner").click()
    owner_form.get_by_label("Owner name").wait_for()
    assert owner_form.get_by_label("Owner name").input_value() == "Taylor"

    page.get_by_role("button", name="Add").click()
    page.get_by_label("Note text").fill("Quick capture from the Add tab.")
    page.get_by_role("button", name="Analyze into records").click()
    page.get_by_label("What would you like to remember?").wait_for()
    assert page.get_by_label("What would you like to remember?").input_value() == \
        "Quick capture from the Add tab."


@pytest.mark.e2e
def test_e2_untrusted_plan_content_is_rendered_as_text(page, ui_server):
    open_capture(page, ui_server())
    submit(page, "xss payload")
    payload = "<img src=x onerror=window.__xss=1>"

    assert page.locator("#capture-plan img").count() == 0
    assert page.locator("#capture-plan").get_by_text(payload, exact=True).count() >= 1
    assert page.evaluate("window.__xss") is None
