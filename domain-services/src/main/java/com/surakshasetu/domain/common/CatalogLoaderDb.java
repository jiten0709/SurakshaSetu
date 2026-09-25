package com.surakshasetu.domain.common;

import java.util.Properties;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * The catalog_loader connection: the only catalog writer, used by the kill switch and the seed
 * loader alone. Every other query runs as domain_rw through the primary datasource. Deliberately
 * not a DataSource bean, so Boot's auto-configured (domain_rw) pool stays the only one.
 */
@Component
public class CatalogLoaderDb {

  // ponytail: unpooled, one connection per call; fine for kill switches and seed runs, pool it
  // if anything hot ever writes the catalog.
  private final JdbcClient jdbc;
  private final TransactionTemplate tx;

  CatalogLoaderDb(
      @Value("${spring.datasource.url}") String url,
      @Value("${surakshasetu.catalog-loader.username}") String username,
      @Value("${surakshasetu.catalog-loader.password}") String password) {
    DriverManagerDataSource dataSource = new DriverManagerDataSource(url, username, password);
    Properties properties = new Properties();
    properties.setProperty("ApplicationName", "domain-services-catalog-loader");
    dataSource.setConnectionProperties(properties);
    this.jdbc = JdbcClient.create(dataSource);
    this.tx = new TransactionTemplate(new DataSourceTransactionManager(dataSource));
  }

  public JdbcClient jdbc() {
    return jdbc;
  }

  public TransactionTemplate tx() {
    return tx;
  }
}
