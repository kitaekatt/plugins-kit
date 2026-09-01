# pdf-kit

Convert local HTML files to PDFs through headless Chromium, with CSS,
JavaScript, web fonts, and browser rendering preserved.

## Cost

Installing pdf-kit downloads the Chromium browser binary during bootstrap.
The download is approximately 180 MB and is stored in Playwright's shared
browser cache.

## Install

```
/plugin marketplace add kitaekatt/plugins-kit
/plugin install pdf-kit@plugins-kit
```

## Use

Invoke `/html-pdf` with a local HTML file. The default output is a single-page
PDF next to the input. Use `--a4` for paginated output.
