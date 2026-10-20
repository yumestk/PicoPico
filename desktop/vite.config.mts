import { defineConfig } from 'vite';

export default defineConfig({
  root: 'renderer', base: './',
  build: { outDir: '../build/renderer', emptyOutDir: true },
});
