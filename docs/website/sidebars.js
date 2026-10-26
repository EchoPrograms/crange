const section = (label, items) => ({type: 'category', label, collapsed: false, items});
module.exports = {handbook: [
  'index',
  section('Getting started', ['start/overview','start/first-lab','start/interface']),
  section('How-to guides', ['how-to/staging','how-to/networks','how-to/releases','how-to/release-tests','how-to/deployments','how-to/machines','how-to/health-checks','how-to/access','how-to/jobs']),
  section('How CRANGE works', ['concepts/architecture','concepts/lifecycle','concepts/networking','concepts/images-storage','concepts/security','concepts/application-boundary']),
  section('Administration', ['admin/controller','admin/projects','admin/templates','admin/capacity','admin/backup-recovery','admin/troubleshooting']),
  section('Reference', ['reference/features','reference/state','reference/api','reference/source-map','reference/glossary','reference/maintaining','generated/cli','generated/operations','generated/api-routes']),
]};
