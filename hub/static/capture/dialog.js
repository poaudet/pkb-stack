export function confirmDialog(title, message, confirmLabel = "Confirm", cancelLabel = "Cancel") {
  const dialog = document.getElementById("app-confirm-dialog");
  dialog.replaceChildren();
  const heading = document.createElement("h2");
  heading.textContent = title;
  const text = document.createElement("p");
  text.textContent = message;
  const confirm = document.createElement("button");
  confirm.type = "button";
  confirm.textContent = confirmLabel;
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "secondary";
  cancel.textContent = cancelLabel;
  dialog.append(heading, text, cancel, confirm);
  return new Promise((resolve) => {
    const finish = (value) => {
      dialog.removeEventListener("close", onClose);
      dialog.close();
      resolve(value);
    };
    const onClose = () => {
      dialog.removeEventListener("close", onClose);
      resolve(false);
    };
    dialog.addEventListener("close", onClose, { once: true });
    const openedAt = Date.now();
    // Ignore the tail of the tap that opened the dialog (mobile ghost click).
    confirm.addEventListener("click", (event) => {
      if (Date.now() - openedAt < 500) { event.preventDefault(); return; }
      finish(true);
    });
    cancel.addEventListener("click", () => finish(false), { once: true });
    dialog.showModal();
    cancel.focus();
  });
}
