package com.example.blog.event;

import org.springframework.context.event.EventListener;
import org.springframework.stereotype.Component;

@Component
public class PostEventListener {

    @EventListener
    public void onPostCreated(PostCreatedEvent event) {
        System.out.println("Post created: " + event.getPost().getTitle());
    }
}
