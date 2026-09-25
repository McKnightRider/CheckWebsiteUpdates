document.addEventListener("DOMContentLoaded", () => {
  const historyItems = document.querySelectorAll(".history-item");
  const historyCount = document.getElementById("history-count");
  if (historyCount) {
    historyCount.textContent = String(historyItems.length);
  }

  for (const element of document.querySelectorAll("[data-checked-at]")) {
    const checkedAt = element.getAttribute("data-checked-at");
    if (checkedAt) {
      element.title = checkedAt;
    }
  }

  const refreshForm = document.getElementById("refresh-form");
  if (!refreshForm) {
    return;
  }

  const endpoint = refreshForm.getAttribute("data-check-now-endpoint") || "";
  const tokenInput = document.getElementById("refresh-token");
  const refreshButton = document.getElementById("refresh-button");
  const refreshStatus = document.getElementById("refresh-status");

  refreshForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!tokenInput || !refreshButton || !refreshStatus) {
      return;
    }

    const token = tokenInput.value;
    if (!endpoint) {
      refreshStatus.textContent = "Refresh is not configured yet.";
      return;
    }
    if (!token) {
      refreshStatus.textContent = "Enter your refresh PIN.";
      return;
    }

    refreshStatus.textContent = "Refreshing…";
    refreshButton.disabled = true;

    try {
      const response = await fetch(endpoint, {
        method: "POST",
        headers: {
          "X-Check-Token": token
        }
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(payload.error || "Refresh failed.");
      }

      const checkedAt = payload?.result?.checked_at || "just now";
      refreshStatus.textContent = `Refresh complete at ${checkedAt}. Reload the page to see the latest site output.`;
    } catch (error) {
      refreshStatus.textContent = error instanceof Error ? error.message : "Refresh failed.";
    } finally {
      refreshButton.disabled = false;
    }
  });
});
