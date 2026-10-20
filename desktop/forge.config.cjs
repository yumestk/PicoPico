const path = require('node:path');

module.exports = {
  packagerConfig: {
    name: 'Pico', executableName: 'Pico', appBundleId: 'dev.picopico.desktop',
    asar: true, appCategoryType: 'public.app-category.developer-tools',
    extraResource: [path.join(__dirname, 'runtime/backend')],
    // Build inputs and private test fixtures never belong in the application.
    ignore: [/^\/(runtime|out|tests|test-results|playwright-report)(\/|$)/],
  },
};
