import { create } from "zustand";

export type Language = "en" | "es";
export type Theme = "dark" | "light";

interface UiState {
  language: Language;
  theme: Theme;
  setLanguage: (language: Language) => void;
  setTheme: (theme: Theme) => void;
}

const storedLanguage = localStorage.getItem("mnemo-language");
const storedTheme = localStorage.getItem("mnemo-theme");

export const useUiStore = create<UiState>((set) => ({
  language:
    storedLanguage === "es" || (!storedLanguage && navigator.language.startsWith("es"))
      ? "es"
      : "en",
  theme: storedTheme === "light" ? "light" : "dark",
  setLanguage: (language) => {
    localStorage.setItem("mnemo-language", language);
    set({ language });
  },
  setTheme: (theme) => {
    localStorage.setItem("mnemo-theme", theme);
    set({ theme });
  },
}));
