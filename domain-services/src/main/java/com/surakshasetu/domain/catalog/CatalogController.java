package com.surakshasetu.domain.catalog;

import static com.surakshasetu.domain.common.Problems.notFound;

import com.surakshasetu.domain.common.BusinessDates;
import com.surakshasetu.domain.common.CatalogLoaderDb;
import com.surakshasetu.domain.contract.CatalogApi;
import com.surakshasetu.domain.contract.model.KillSwitch;
import com.surakshasetu.domain.contract.model.KillSwitchResult;
import com.surakshasetu.domain.contract.model.Product;
import com.surakshasetu.domain.contract.model.ProductStatus;
import com.surakshasetu.domain.contract.model.ProductType;
import com.surakshasetu.domain.contract.model.Rider;
import java.time.OffsetDateTime;
import java.util.List;
import org.jspecify.annotations.Nullable;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.RestController;

@RestController
class CatalogController implements CatalogApi {

  private static final Logger log = LoggerFactory.getLogger(CatalogController.class);

  private final CatalogRepository catalog;
  private final CatalogLoaderDb catalogLoader;

  CatalogController(CatalogRepository catalog, CatalogLoaderDb catalogLoader) {
    this.catalog = catalog;
    this.catalogLoader = catalogLoader;
  }

  @Override
  public ResponseEntity<List<Product>> listProducts(
      @Nullable ProductStatus status,
      @Nullable ProductType category,
      @Nullable Boolean launchEnabled,
      @Nullable OffsetDateTime asOf) {
    return ResponseEntity.ok(
        catalog.products(status, category, launchEnabled, BusinessDates.on(asOf)));
  }

  @Override
  public ResponseEntity<Product> getProduct(String uin, @Nullable OffsetDateTime asOf) {
    return ResponseEntity.ok(
        catalog
            .product(uin, BusinessDates.on(asOf))
            .orElseThrow(() -> notFound("no product with this UIN in force on as_of")));
  }

  @Override
  public ResponseEntity<Rider> getRider(String uin) {
    return ResponseEntity.ok(
        catalog.rider(uin).orElseThrow(() -> notFound("no rider with this UIN")));
  }

  /**
   * Withdraws a product from sale today (IST). Idempotent: a product already withdrawn keeps its
   * earlier effective_to. The orchestrator audits the call (KILL_SWITCH); this tier never audits.
   */
  // Carry-over: "internal scope" is not enforced yet; this tier verifies no token at all.
  @Override
  public ResponseEntity<KillSwitchResult> setProductKillSwitch(String uin, KillSwitch killSwitch) {
    int updated =
        catalogLoader
            .jdbc()
            .sql(
                """
                UPDATE catalog.product SET status = 'withdrawn',
                  effective_to = LEAST(COALESCE(effective_to, :today), :today)
                WHERE uin = :uin
                """)
            .param("today", BusinessDates.on(null))
            .param("uin", uin)
            .update();
    if (updated == 0) {
      throw notFound("no product with this UIN");
    }
    log.info("kill switch: {} withdrawn", uin);
    return ResponseEntity.ok(new KillSwitchResult(uin, ProductStatus.WITHDRAWN));
  }
}
