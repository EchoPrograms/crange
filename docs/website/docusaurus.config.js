// Static local documentation; no analytics, remote fonts or hosted search.
module.exports = {
  title: 'CRANGE', tagline: 'Build, release and run your cyber range',
  url: 'http://localhost:8769', baseUrl: '/', favicon: 'img/favicon.svg',
  organizationName: 'EchoPrograms', projectName: 'crange',
  onBrokenLinks: 'throw',
  markdown: {format: 'md', hooks: {onBrokenMarkdownLinks: 'throw'}},
  trailingSlash: true,
  presets: [['classic', {
    docs: {path: '../handbook', routeBasePath: '/', sidebarPath: './sidebars.js'},
    blog: false, pages: false,
    theme: {customCss: './src/css/custom.css'},
  }]],
  themes: [['@easyops-cn/docusaurus-search-local', {
    hashed: true, language: 'en', indexBlog: false, indexPages: false,
    docsRouteBasePath: '/', docsDir: '../handbook', highlightSearchTermsOnTargetPage: true,
  }]],
  themeConfig: {
    colorMode: {defaultMode: 'light', respectPrefersColorScheme: true},
    navbar: {title: 'CRANGE', logo: {alt: '', src: 'img/favicon.svg'}, items: [
      {to: '/start/first-lab/', label: 'Get started', position: 'left'},
      {to: '/reference/features/', label: 'Features', position: 'left'},
      {to: '/admin/controller/', label: 'Administration', position: 'left'},
    ]},
    docs: {sidebar: {hideable: true, autoCollapseCategories: false}},
    footer: {style: 'dark', copyright: 'CRANGE handbook · Local documentation'},
  },
};
