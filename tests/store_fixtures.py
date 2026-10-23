"""Shared test fixture: the checkpoint store invariant, every object referenced by a saved manifest."""
from devcontainer_setup.project_sync import checkpoint_store


def unreferenced_objects(store):
    """Digests of `store` objects that no saved manifest references. The store never deletes, so each one leaks."""
    def names(family):
        directory = store.root / family
        return {path.name for path in directory.iterdir()} if directory.is_dir() else set()
    referenced = set()
    for snapshot_id in names('manifests'):
        referenced.update(checkpoint_store._references(store.load(snapshot_id)))
    return names('objects') - referenced


def assert_every_object_referenced(test, store):
    unreferenced = unreferenced_objects(store)
    test.assertEqual(unreferenced, set(), f'{len(unreferenced)} unreferenced objects in {store.root}: '
                     + ', '.join(f'{digest[:12]} {len(store.read(digest))} B' for digest in sorted(unreferenced)))
