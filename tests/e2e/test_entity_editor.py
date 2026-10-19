import pytest

from tests.e2e.conftest import TOKEN


def open_app(page, base_url):
    def handle_dialog(dialog):
        dialog.accept(TOKEN if dialog.type == "prompt" else None)

    page.on("dialog", handle_dialog)
    page.goto(base_url)
    page.locator("#capture-privacy").wait_for()


@pytest.mark.e2e
def test_entity_editor_diff_and_schema_controls(page, ui_server):
    open_app(page, ui_server())
    result = page.evaluate("""async () => {
      const { createEntityEditor, diffEntity } = await import('/static/entity-editor.mjs');
      const schema = {type: 'object', required: ['name'], properties: {
        name: {type: 'string'},
        long_text: {type: 'string', maxLength: 500},
        birthday: {type: 'string', format: 'date'},
        at: {type: 'string', format: 'date-time'},
        direction: {type: 'string', enum: ['in', 'out']},
        enabled: {type: 'boolean'},
        count: {type: 'integer', minimum: 1, maximum: 8},
        labels: {type: 'array', items: {type: 'string'}},
        contact: {type: 'object', properties: {email: {type: 'string'}}},
        reference: {type: 'string', 'x-ref': 'person'},
        custom: {type: 'object', additionalProperties: true}
      }};
      const editor = createEntityEditor(schema, {
        name: 'A', birthday: '2026-10-18', at: '2026-10-18T12:00:00Z',
        direction: 'out', enabled: true, count: 3, labels: ['a', 'b'],
        contact: {email: 'a@example.test'}, reference: 'uid'
      }, {person: [{id: 'uid', label: 'Alice'}]});
      const controls = Object.fromEntries([...editor.form.querySelectorAll('[id^="entity-field-"]')]
        .map((input) => [input.id.slice('entity-field-'.length),
          input.tagName === 'FIELDSET' ? input.tagName : input.type]));
      const delta = diffEntity({required: 'keep', optional: 'old', labels: ['a']},
        {required: 'keep', labels: ['b']}, ['required']);
      return {controls, delta,
        requiredLabel: editor.form.querySelector('label[for="entity-field-name"]').textContent,
        datalist: editor.form.querySelector('#entity-field-reference + datalist option')?.label,
        minimum: editor.form.querySelector('#entity-field-count').min};
    }""")
    assert result["controls"] == {
        "name": "text", "long_text": "textarea", "birthday": "date",
        "at": "datetime-local", "direction": "select-one", "enabled": "checkbox",
        "count": "number", "labels": "textarea", "contact": "FIELDSET",
        "reference": "text", "custom": "textarea",
    }
    assert result["delta"] == {"optional": None, "labels": ["b"]}
    assert result["requiredLabel"] == "name *"
    assert result["datalist"] == "Alice"
    assert result["minimum"] == "1"


@pytest.mark.e2e
def test_ui_edit_handles_stale_entity_reload_and_save(page, ui_server):
    base_url = ui_server()
    open_app(page, base_url)
    entity = page.evaluate("""async () => {
      const response = await fetch('/api/entities', {
        method: 'POST', headers: {'Content-Type': 'application/json',
          Authorization: 'Bearer ' + localStorage.getItem('hub_token')},
        body: JSON.stringify({type: 'person',
          data: {name: 'Edit me', relationship: 'friend', notes: 'before'}})
      });
      return (await response.json()).entity;
    }""")
    page.goto(f"{base_url}/#entity/{entity['id']}")
    page.locator("#entity-dialog").get_by_role("button", name="Edit").wait_for()
    page.locator("#entity-dialog").get_by_role("button", name="Edit").click()
    relationship = page.locator("#entity-field-relationship")
    relationship.fill("colleague")
    page.evaluate("""async (uid) => fetch(`/api/entities/${uid}`, {
      method: 'PATCH', headers: {'Content-Type': 'application/json',
        Authorization: 'Bearer ' + localStorage.getItem('hub_token')},
      body: JSON.stringify({notes: 'concurrent update'})
    })""", entity["id"])
    page.get_by_role("button", name="Save changes").click()
    page.get_by_text("entity changed since it was loaded").wait_for()
    page.get_by_role("button", name="Reload").click()
    page.locator("#entity-dialog").get_by_role("button", name="Edit").wait_for()
    page.locator("#entity-dialog").get_by_role("button", name="Edit").click()
    page.locator("#entity-field-relationship").fill("colleague")
    page.get_by_role("button", name="Save changes").click()
    page.locator("#entity-dialog pre").get_by_text("colleague").wait_for()
    assert page.locator("#entity-dialog pre").get_by_text("concurrent update").is_visible()


@pytest.mark.e2e
def test_type_delete_dialog_exports_and_converts_entities(page, ui_server):
    open_app(page, ui_server())
    page.evaluate("""async () => {
      const headers = {'Content-Type': 'application/json',
        Authorization: 'Bearer ' + localStorage.getItem('hub_token')};
      await fetch('/api/entity-types', {method: 'POST', headers, body: JSON.stringify({
        name: 'custom_ui',
        json_schema: {type: 'object', properties: {title: {type: 'string'}}}
      })});
      await fetch('/api/entities', {method: 'POST', headers, body: JSON.stringify({
        type: 'custom_ui', data: {title: 'Keep this as a note'}
      })});
    }""")
    page.get_by_role("button", name="Entities").click()
    page.locator("#e-type").select_option("custom_ui")
    page.locator("#e-type-details summary").click()
    page.get_by_role("button", name="Delete type…").click()
    page.get_by_role("dialog").get_by_text("1 current · 0 archived · 0 superseded").wait_for()
    with page.expect_download() as download:
        page.get_by_role("button", name="Export JSON first").click()
    assert download.value.suggested_filename == "custom_ui-entities.json"
    page.get_by_label("Also permanently delete 1 entity").check()
    page.get_by_label("Convert entities to notes before deleting").check()
    page.get_by_label("Type the entity type name to confirm").fill("custom_ui")
    page.get_by_role("button", name="Delete type", exact=True).click()
    page.wait_for_function(
        "() => ![...document.querySelector('#e-type').options].some(option => option.value === 'custom_ui')"
    )
    note = page.evaluate("""async () => {
      const headers = {Authorization: 'Bearer ' + localStorage.getItem('hub_token')};
      const list = await (await fetch('/api/notes?q=Keep%20this%20as%20a%20note', {headers})).json();
      return (await (await fetch('/api/notes/' + list.items[0].id, {headers})).json()).note;
    }""")
    assert "Keep this as a note" in note["content"]


@pytest.mark.e2e
def test_type_policy_locks_and_unlocks_schema_extensions(page, ui_server):
    open_app(page, ui_server())
    page.evaluate("""async () => {
      const headers = {'Content-Type': 'application/json',
        Authorization: 'Bearer ' + localStorage.getItem('hub_token')};
      await fetch('/api/entity-types', {method: 'POST', headers, body: JSON.stringify({
        name: 'fixed_schema_ui',
        json_schema: {type: 'object', properties: {title: {type: 'string'}}},
        policy: {allow_extension: false}
      })});
    }""")
    page.get_by_role("button", name="Entities").click()
    page.locator("#e-type").select_option("fixed_schema_ui")
    page.locator("#e-type-details summary").click()
    assert page.get_by_role("button", name="Schema locked").is_disabled()
    page.locator("#e-type-details label").filter(
        has_text="allow extension"
    ).locator("input").check()
    page.get_by_role("button", name="Save policies").click()
    page.locator("#e-type-details summary").click()
    page.get_by_role("button", name="Add field").wait_for()
    assert page.get_by_role("button", name="Add field").is_enabled()
