import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    outDir: "../katsi_app/static",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": process.env.KATSI_API_URL ?? "http://localhost:8000",
    },
  },
});
