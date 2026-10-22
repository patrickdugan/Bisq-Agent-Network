# Verified upstream integration references

Inspected from the upstream bisq-network/bisq2 repository on October 22, 2026.

- https://github.com/bisq-network/bisq2/blob/main/api/usage.md : localhost REST API, /api/v1, and interactive Swagger UI.
- https://github.com/bisq-network/bisq2/blob/main/api/src/main/java/bisq/api/rest_api/endpoints/offers/OfferbookRestApi.java : GET /offerbook/markets/{currencyCode}/offers.
- https://github.com/bisq-network/bisq2/blob/main/api/src/main/java/bisq/api/dto/presentation/offerbook/OfferItemPresentationDto.java : list wrapper with presentation fields.
- https://github.com/bisq-network/bisq2/blob/main/api/src/main/java/bisq/api/dto/offer/bisq_easy/BisqEasyOfferDto.java : maker direction, amount and price spec.
- https://github.com/bisq-network/bisq2/blob/main/api/src/main/java/bisq/api/dto/common/monetary/PriceQuoteDto.java : structured price value and precision.
- https://github.com/bisq-network/bisq2/blob/main/api/src/main/java/bisq/api/access/filter/Headers.java : Bisq-Client-Id and Bisq-Session-Id.
- https://github.com/bisq-network/bisq2/blob/main/api/architecture.md : node pairing, transport security and authorization.
- https://github.com/zkp2p : independent payment-attestation research and SDKs; NOT integrated into this codebase.

API contracts can change across Bisq 2 releases. Compare them with the version of the node you connect and pin reviewed releases for production work. This project incorporates no upstream source files.
