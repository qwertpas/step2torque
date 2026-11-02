# Viewer

[Open the viewer](https://qwertpas.github.io/step2torque/). Choose an example or
open a local `.s2t` solution. Nothing is uploaded.

Browser sources are in `src/`; portable public fields are in `examples/`.
The Python viewer lives in `src/step2torque/visualization/` at the repository root.

From this directory:

```sh
npm ci
npm run build
npm test
npm run test:browser
npm run test:site
```

The build updates the bundled Python renderer and writes the static site to
`build/site/` at the repository root. Open its `index.html` in your browser.
The browser tests require the runtime installed by `npx playwright install chromium`.

The Pages workflow builds and deploys this folder on pushes to `main`.
Set the repository's Pages publishing source to GitHub Actions.
