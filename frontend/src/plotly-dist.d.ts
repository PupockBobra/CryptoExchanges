// plotly.js-dist-min ships the same API as plotly.js but as a pre-bundled
// minified file without its own TypeScript declarations.
// Re-use the @types/plotly.js declarations so the import is fully typed.
declare module 'plotly.js-dist-min' {
  import * as Plotly from 'plotly.js'
  export = Plotly
}
