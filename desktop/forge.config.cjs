const path = require('node:path');
const fs = require('node:fs/promises');

module.exports = {
  packagerConfig: {
    name: 'Pico', executableName: 'Pico', appBundleId: 'dev.picopico.desktop',
    asar: true, appCategoryType: 'public.app-category.developer-tools',
    // The default extraResource copy resolves relative links into developer paths.
    extraResource: [],
    afterCopyExtraResources: [async ({ buildPath }) => {
      await fs.cp(path.join(__dirname, 'runtime/backend'), path.join(buildPath, 'Pico.app/Contents/Resources/backend'),
        { recursive: true, verbatimSymlinks: true });
    }],
    // Build inputs and private test fixtures never belong in the application.
    ignore: [/^\/(runtime|out|tests|test-results|playwright-report)(\/|$)/],
  },
};
