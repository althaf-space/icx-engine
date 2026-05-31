package com.example.blog.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties(prefix = "app")
public class AppProperties {
    private String name;
    private int maxPosts;

    public String getName() { return name; }
    public void setName(String name) { this.name = name; }
    public int getMaxPosts() { return maxPosts; }
    public void setMaxPosts(int maxPosts) { this.maxPosts = maxPosts; }
}
