import { useCallback, useEffect, useRef, useState } from "react";

type SpeechResult = { isFinal: boolean; 0: { transcript: string } };
type SpeechEvent = { resultIndex: number; results: ArrayLike<SpeechResult> };
type SpeechError = { error: string };
type BrowserRecognition = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  onresult: ((event: SpeechEvent) => void) | null;
  onerror: ((event: SpeechError) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
};
type RecognitionWindow = Window & {
  SpeechRecognition?: new () => BrowserRecognition;
  webkitSpeechRecognition?: new () => BrowserRecognition;
};

export function useSpeechInput(onFinal: (transcript: string) => void) {
  const [supported, setSupported] = useState(false);
  const [listening, setListening] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const recognition = useRef<BrowserRecognition | null>(null);
  const onFinalRef = useRef(onFinal);

  useEffect(() => {
    onFinalRef.current = onFinal;
  }, [onFinal]);

  useEffect(() => {
    const browser = window as RecognitionWindow;
    const frame = requestAnimationFrame(() => {
      setSupported(Boolean(browser.SpeechRecognition ?? browser.webkitSpeechRecognition));
    });
    return () => {
      cancelAnimationFrame(frame);
      recognition.current?.abort();
      recognition.current = null;
    };
  }, []);

  const toggle = useCallback(() => {
    if (recognition.current) {
      recognition.current.stop();
      return;
    }
    const browser = window as RecognitionWindow;
    const Constructor = browser.SpeechRecognition ?? browser.webkitSpeechRecognition;
    if (!Constructor) {
      setError("Voice input is not supported in this browser. Type your message instead.");
      return;
    }
    const instance = new Constructor();
    const committed = new Set<number>();
    instance.lang = navigator.language || "en-US";
    instance.continuous = false;
    instance.interimResults = false;
    instance.onresult = (event) => {
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        const result = event.results[index];
        if (result.isFinal && !committed.has(index)) {
          committed.add(index);
          onFinalRef.current(result[0].transcript);
        }
      }
    };
    instance.onerror = (event) => {
      setError(
        event.error === "not-allowed" || event.error === "service-not-allowed"
          ? "Microphone permission was denied. Allow microphone access or type your message."
          : "Speech recognition failed. You can retry or type your message.",
      );
    };
    instance.onend = () => {
      recognition.current = null;
      setListening(false);
    };
    try {
      instance.start();
      recognition.current = instance;
      setListening(true);
      setError(null);
    } catch {
      setError("Speech recognition could not start. You can retry or type your message.");
      setListening(false);
    }
  }, []);

  const cancel = useCallback(() => {
    recognition.current?.abort();
    recognition.current = null;
    setListening(false);
  }, []);

  return { supported, listening, error, toggle, cancel };
}
