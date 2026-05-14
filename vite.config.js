import { defineConfig, normalizePath } from 'vite';
import { viteStaticCopy } from 'vite-plugin-static-copy';
import path from 'path';

export default defineConfig({
  plugins: [
    viteStaticCopy({
      targets: [
        {
          src: normalizePath(path.resolve(__dirname, 'questions/questions.json')),
          dest: ''
        }
      ]
    })
  ]
});