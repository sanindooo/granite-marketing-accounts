"use client";

import { useCallback, useRef, useState } from "react";
import type { PipelineCommand, PipelineOptions } from "@/lib/actions/pipeline";

export interface ProgressEvent {
  event: "progress";
  stage: string;
  current: number;
  total: number;
  detail: string;
}

export interface CompleteEvent {
  event: "complete";
  result: Record<string, unknown>;
}

export interface ErrorEvent {
  event: "error";
  message: string;
  error_code?: string;
  user_message?: string;
}

export type StreamEvent = ProgressEvent | CompleteEvent | ErrorEvent;

export interface PipelineStreamState {
  isRunning: boolean;
  activeCommand: PipelineCommand | null;
  progress: ProgressEvent | null;
  result: CompleteEvent["result"] | null;
  error: ErrorEvent | null;
}

export function usePipelineStream() {
  const [state, setState] = useState<PipelineStreamState>({
    isRunning: false,
    activeCommand: null,
    progress: null,
    result: null,
    error: null,
  });

  const abortControllerRef = useRef<AbortController | null>(null);

  const run = useCallback(
    async (command: PipelineCommand, options?: PipelineOptions) => {
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }

      abortControllerRef.current = new AbortController();

      setState({
        isRunning: true,
        activeCommand: command,
        progress: null,
        result: null,
        error: null,
      });

      try {
        const response = await fetch("/api/pipeline/stream", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ command, ...options }),
          signal: abortControllerRef.current.signal,
        });

        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }

        const reader = response.body?.getReader();
        if (!reader) {
          throw new Error("No response body");
        }

        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n\n");
          buffer = lines.pop() || "";

          for (const line of lines) {
            if (!line.startsWith("data: ")) continue;
            const data = line.slice(6);
            try {
              const event = JSON.parse(data) as StreamEvent;

              if (event.event === "progress") {
                setState((prev) => ({ ...prev, progress: event }));
              } else if (event.event === "complete") {
                setState({
                  isRunning: false,
                  activeCommand: null,
                  progress: null,
                  result: event.result,
                  error: null,
                });
              } else if (event.event === "error") {
                setState({
                  isRunning: false,
                  activeCommand: null,
                  progress: null,
                  result: null,
                  error: event,
                });
              }
            } catch {
              // Ignore parse errors
            }
          }
        }
      } catch (err) {
        if (err instanceof Error && err.name === "AbortError") {
          setState((prev) => ({ ...prev, isRunning: false, activeCommand: null }));
          return;
        }
        setState({
          isRunning: false,
          activeCommand: null,
          progress: null,
          result: null,
          error: {
            event: "error",
            message: err instanceof Error ? err.message : "Unknown error",
          },
        });
      }
    },
    []
  );

  const cancel = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    setState((prev) => ({ ...prev, isRunning: false, activeCommand: null }));
  }, []);

  return { ...state, run, cancel };
}
