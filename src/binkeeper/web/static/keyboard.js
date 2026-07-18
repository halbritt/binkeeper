(function () {
  "use strict";

  var lastHelpTrigger = null;

  function helpModal() {
    return document.getElementById("help-modal");
  }

  function isTextEntry(element) {
    if (!element) {
      return false;
    }
    var tagName = element.tagName;
    return (
      tagName === "INPUT" ||
      tagName === "TEXTAREA" ||
      tagName === "SELECT" ||
      element.isContentEditable === true
    );
  }

  function openHelp(trigger) {
    var modal = helpModal();
    if (!modal) {
      return;
    }
    lastHelpTrigger = trigger || document.activeElement;
    modal.hidden = false;
    var heading = modal.querySelector("h2, [tabindex]");
    if (heading && typeof heading.focus === "function") {
      if (!heading.hasAttribute("tabindex")) {
        heading.setAttribute("tabindex", "-1");
      }
      heading.focus();
    }
  }

  function closeHelp() {
    var modal = helpModal();
    if (!modal) {
      return;
    }
    modal.hidden = true;
    if (lastHelpTrigger && typeof lastHelpTrigger.focus === "function") {
      lastHelpTrigger.focus();
    }
  }

  function elementForKey(key) {
    var elements = document.querySelectorAll("[data-key]");
    for (var i = 0; i < elements.length; i += 1) {
      if (elements[i].getAttribute("data-key") === key && !elements[i].disabled) {
        return elements[i];
      }
    }
    return null;
  }

  function markCopied(button) {
    button.setAttribute("data-copy-state", "copied");
    button.textContent = "copied";
  }

  function copyCommand(button) {
    var command = button.getAttribute("data-copy-command");
    if (!command) {
      return;
    }

    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      navigator.clipboard.writeText(command).then(function () {
        markCopied(button);
      }).catch(function () {
        button.setAttribute("data-copy-state", "error");
      });
    }
  }

  document.addEventListener("click", function (event) {
    var copyButton = event.target.closest("[data-copy-command]");
    if (copyButton) {
      event.preventDefault();
      copyCommand(copyButton);
      return;
    }

    var opener = event.target.closest("[data-help-open]");
    if (opener) {
      openHelp(opener);
      return;
    }
    if (event.target.closest("[data-help-close]")) {
      closeHelp();
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      closeHelp();
      return;
    }

    if (isTextEntry(document.activeElement)) {
      return;
    }

    if (event.key === "?" || (event.shiftKey && event.key === "/")) {
      event.preventDefault();
      openHelp(document.activeElement);
      return;
    }

    var target = elementForKey(event.key);
    if (!target) {
      return;
    }
    event.preventDefault();
    target.click();
  });

  document.body.addEventListener("htmx:afterSwap", function () {
    var live = document.getElementById("live-region");
    var status = document.querySelector("[data-live-status]");
    if (live && status) {
      live.textContent = status.getAttribute("data-live-status");
    }
    var heading = document.querySelector('h2[tabindex="-1"]');
    if (heading && typeof heading.focus === "function") {
      heading.focus();
    }
  });
})();
