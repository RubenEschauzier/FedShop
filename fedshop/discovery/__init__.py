"""Discovery-based FedShop: a marketplace in which every triple has one owner.

Brands own their products, a taxonomy owns product types and features, shops and review sites own
their listings, offers and reviews, and reviewers own their profiles. Every owner publishes
documents (served by the Community Solid Server); the larger ones also run a query service that
claims their namespace. An engine starts from a catalog of known services and discovers the rest
through references in the data. See experiments/bsbm/discovery/README.md.
"""
