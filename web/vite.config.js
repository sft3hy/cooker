import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

// base './' because the built assets are served by the daemon at whatever
// root the tailnet route happens to mount — a root-absolute base breaks the
// moment Traefik adds a prefix, and this project is served through Traefik.
export default defineConfig({
  base: './',
  // the vendored font and its OFL license live in static/, and Vite only
  // copies `publicDir` verbatim — saying so is cheaper than moving the files
  // somewhere less honest-looking than "static".
  publicDir: 'static',
  plugins: [svelte()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    // dev convenience: `npm run dev` hits the daemon's API directly
    proxy: {
      '/api': 'http://127.0.0.1:8256',
    },
  },
});
