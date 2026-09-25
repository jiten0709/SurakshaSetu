package com.surakshasetu.domain.catalog;

import com.surakshasetu.domain.contract.model.PptOption;
import com.surakshasetu.domain.contract.model.Product;
import com.surakshasetu.domain.contract.model.ProductDocument;
import com.surakshasetu.domain.contract.model.ProductStatus;
import com.surakshasetu.domain.contract.model.ProductType;
import com.surakshasetu.domain.contract.model.Rider;
import java.math.BigDecimal;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.LocalDate;
import java.util.Arrays;
import java.util.HexFormat;
import java.util.List;
import java.util.Optional;
import org.jspecify.annotations.Nullable;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

/**
 * Reads the Product Catalog as domain_rw. A product is served on a date inside [effective_from,
 * effective_to), so a withdrawn product only answers for a date before its effective_to.
 */
@Repository
public class CatalogRepository {

  private static final String PRODUCT_COLUMNS =
      """
      SELECT uin, name, category, status, entry_age_min, entry_age_max, maturity_age_max,
        sa_min_inr, sa_max_inr, lower(term_years) AS term_min, upper(term_years) - 1 AS term_max,
        ppt_options, payout_options, rider_uins, effective_from, effective_to, launch_enabled,
        is_dummy
      FROM catalog.product
      WHERE effective_from <= :on AND (effective_to IS NULL OR :on < effective_to)
      """;

  private final JdbcClient jdbc;

  CatalogRepository(JdbcClient jdbc) {
    this.jdbc = jdbc;
  }

  public List<Product> products(
      @Nullable ProductStatus status,
      @Nullable ProductType category,
      @Nullable Boolean launchEnabled,
      LocalDate on) {
    return jdbc.sql(
            PRODUCT_COLUMNS
                + """
                  AND (CAST(:status AS text) IS NULL OR status = :status)
                  AND (CAST(:category AS text) IS NULL OR category = :category)
                  AND (CAST(:launch AS boolean) IS NULL OR launch_enabled = :launch)
                ORDER BY uin
                """)
        .param("on", on)
        .param("status", status == null ? null : status.getValue())
        .param("category", category == null ? null : category.getValue())
        .param("launch", launchEnabled)
        .query(this::product)
        .list();
  }

  public Optional<Product> product(String uin, LocalDate on) {
    return jdbc.sql(PRODUCT_COLUMNS + " AND uin = :uin")
        .param("on", on)
        .param("uin", uin)
        .query(this::product)
        .optional();
  }

  public boolean exists(String uin) {
    return jdbc.sql("SELECT EXISTS (SELECT 1 FROM catalog.product WHERE uin = ?)")
        .param(uin)
        .query(Boolean.class)
        .single();
  }

  public Optional<Rider> rider(String uin) {
    return jdbc.sql(
            "SELECT uin, name, attaches_to, sa_max_inr, is_dummy FROM catalog.rider WHERE uin = ?")
        .param(uin)
        .query(CatalogRepository::rider)
        .optional();
  }

  private Product product(ResultSet rs, int row) throws SQLException {
    String uin = rs.getString("uin");
    return new Product(
        uin,
        rs.getString("name"),
        ProductType.fromValue(rs.getString("category")),
        ProductStatus.fromValue(rs.getString("status")),
        rs.getInt("entry_age_min"),
        rs.getInt("entry_age_max"),
        rs.getInt("maturity_age_max"),
        money(rs.getBigDecimal("sa_min_inr")),
        money(rs.getBigDecimal("sa_max_inr")),
        rs.getInt("term_min"),
        rs.getInt("term_max"),
        strings(rs, "ppt_options").stream().map(PptOption::fromValue).toList(),
        strings(rs, "payout_options"), // contract name: benefit_payment_options
        strings(rs, "rider_uins"),
        rs.getObject("effective_from", LocalDate.class),
        rs.getObject("effective_to", LocalDate.class),
        rs.getBoolean("launch_enabled"),
        rs.getBoolean("is_dummy"),
        riders(uin),
        documents(uin));
  }

  // ponytail: two queries per product (riders, documents); fine for a catalog of a few products,
  // batch them by UIN if the catalog grows.
  private List<Rider> riders(String productUin) {
    return jdbc.sql(
            """
            SELECT r.uin, r.name, r.attaches_to, r.sa_max_inr, r.is_dummy
            FROM catalog.product p JOIN catalog.rider r ON r.uin = ANY (p.rider_uins)
            WHERE p.uin = ? ORDER BY r.uin
            """)
        .param(productUin)
        .query(CatalogRepository::rider)
        .list();
  }

  private List<ProductDocument> documents(String productUin) {
    return jdbc.sql(
            """
            SELECT kind, version, language, uri, sha256 FROM catalog.product_document
            WHERE uin = ? ORDER BY kind, version, language
            """)
        .param(productUin)
        .query(
            (rs, n) ->
                new ProductDocument(
                    ProductDocument.KindEnum.fromValue(rs.getString("kind")),
                    rs.getString("version"),
                    rs.getString("language"),
                    rs.getString("uri"),
                    HexFormat.of().formatHex(rs.getBytes("sha256"))))
        .list();
  }

  private static Rider rider(ResultSet rs, int row) throws SQLException {
    return new Rider(
        rs.getString("uin"),
        rs.getString("name"),
        strings(rs, "attaches_to"),
        money(rs.getBigDecimal("sa_max_inr")),
        rs.getBoolean("is_dummy"));
  }

  private static List<String> strings(ResultSet rs, String column) throws SQLException {
    return Arrays.asList((String[]) rs.getArray(column).getArray());
  }

  private static @Nullable String money(@Nullable BigDecimal amount) {
    return amount == null ? null : amount.toPlainString();
  }
}
