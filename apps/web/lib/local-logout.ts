"use client";

export function submitLocalLogout(scope: "current" | "all" = "current"): void {
  const form = document.createElement("form");
  form.method = "POST";
  form.action = `/api/auth/local-logout?scope=${scope}`;
  form.hidden = true;
  document.body.append(form);
  form.submit();
}
