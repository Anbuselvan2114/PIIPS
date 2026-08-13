import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build to ./dist. Served by FastAPI at the site root ("/"), so the
// default absolute asset base ("/") is correct behind the IIS proxy.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
