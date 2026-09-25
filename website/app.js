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
});
