"use client";

import { useEffect, useState } from "react";
import { parseSkin, type AgentSkin } from "./skins";

export type Theme = "light" | "dark";
export function useTheme() {
  const [theme, setThemeState] = useState<Theme>("light");
  const [skin, setSkinState] = useState<AgentSkin>("sage");
  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      let stored: string | null = null;
      let storedSkin: string | null = null;
      try {
        stored = window.localStorage.getItem("talos-theme");
        storedSkin = window.localStorage.getItem("talos-skin");
      } catch { /* Storage may be blocked; use session preferences. */ }
      const initialTheme: Theme = stored === "light" || stored === "dark"
        ? stored : window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
      const initialSkin = parseSkin(storedSkin);
      setThemeState(initialTheme);
      setSkinState(initialSkin);
      document.documentElement.dataset.theme = initialTheme;
      document.documentElement.dataset.skin = initialSkin;
      document.documentElement.style.colorScheme = initialTheme;
    });
    return () => window.cancelAnimationFrame(frame);
  }, []);

  function setTheme(next: Theme) {
    setThemeState(next);
    document.documentElement.dataset.theme = next;
    document.documentElement.style.colorScheme = next;
    try { window.localStorage.setItem("talos-theme", next); } catch { /* Session only. */ }
  }
  function setSkin(next: AgentSkin) {
    setSkinState(next);
    document.documentElement.dataset.skin = next;
    try { window.localStorage.setItem("talos-skin", next); } catch { /* Session only. */ }
  }
  function toggleTheme() { setTheme(theme === "light" ? "dark" : "light"); }
  return { theme, toggleTheme, setTheme, skin, setSkin };
}
