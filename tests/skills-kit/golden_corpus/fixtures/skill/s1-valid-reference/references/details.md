# Widget limit details

The limit of 42 widgets per batch comes from the fixture allocator's page
size. Batches beyond the limit return E_LIMIT and are not partially applied.
