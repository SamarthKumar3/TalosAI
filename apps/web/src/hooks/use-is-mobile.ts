"use client";

import { useSyncExternalStore } from "react";

const QUERY = "(max-width: 860px)";
function subscribe(listener: () => void) {
  const media = window.matchMedia(QUERY);
  media.addEventListener("change", listener);
  return () => media.removeEventListener("change", listener);
}
function snapshot() { return window.matchMedia(QUERY).matches; }
function serverSnapshot() { return false; }

export function useIsMobile() { return useSyncExternalStore(subscribe, snapshot, serverSnapshot); }
