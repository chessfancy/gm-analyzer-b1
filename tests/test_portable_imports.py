def test_production_pipeline_imports_as_package():
    from chessgrandmaster import production_pipeline

    assert production_pipeline.PIPELINE_VERSION == 2
