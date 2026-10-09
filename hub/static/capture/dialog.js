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
  dialog.append(heading, text, confirm, cancel);
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
    confirm.addEventListener("click", () => finish(true), { once: true });
    cancel.addEventListener("click", () => finish(false), { once: true });
    dialog.showModal();
  });
}
