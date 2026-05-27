package com.example.blog.scheduler;

import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

import com.example.blog.service.PostService;

@Component
public class CleanupScheduler {
    private final PostService postService;

    public CleanupScheduler(PostService postService) {
        this.postService = postService;
    }

    @Scheduled(fixedRate = 3600000)
    public void cleanDrafts() {
        postService.deleteStaleDrafts();
    }
}
