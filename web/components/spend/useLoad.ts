"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export type LoadState<T> =
  | { status: "loading" }
  | { status: "error"; error: unknown }
  | { status: "ready"; data: T };

/**
 * Run `load` whenever `key` changes (or `retry()` is called) and track loading / error / ready.
 * A result that arrives after the key has moved on is dropped, so a slow answer for an old month
 * never overwrites the current one. Pass `key = null` to wait (state stays "loading").
 */
export function useLoad<T>(key: string | null, load: () => Promise<T>): LoadState<T> & { retry: () => void } {
  const loadRef = useRef(load);
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<{ token: string; state: LoadState<T> } | null>(null);
  const token = key === null ? null : `${attempt}\u0000${key}`;

  useEffect(() => {
    loadRef.current = load;
  });

  useEffect(() => {
    if (token === null) return;
    let live = true;
    loadRef.current().then(
      (data) => live && setResult({ token, state: { status: "ready", data } }),
      (error: unknown) => live && setResult({ token, state: { status: "error", error } }),
    );
    return () => {
      live = false;
    };
  }, [token]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);
  const state: LoadState<T> = result && result.token === token ? result.state : { status: "loading" };
  return { ...state, retry };
}
