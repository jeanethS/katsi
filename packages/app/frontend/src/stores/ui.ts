import { create } from "zustand";

export type Language = "en" | "es";
export type Theme = "dark" | "light";

interface UiState {
  language: Language;
  theme: Theme;
  setLanguage: (language: Language) => void;
  setTheme: (theme: Theme) => void;
}

const storedLanguage = localStorage.getItem("katsi-language");
const storedTheme = localStorage.getItem("katsi-theme");

export const useUiStore = create<UiState>((set) => ({
  language:
    storedLanguage === "es" || (!storedLanguage && navigator.language.startsWith("es"))
      ? "es"
      : "en",
  theme: storedTheme === "light" ? "light" : "dark",
  setLanguage: (language) => {
    localStorage.setItem("katsi-language", language);
    set({ language });
  },
  setTheme: (theme) => {
    localStorage.setItem("katsi-theme", theme);
    set({ theme });
  },
}));
